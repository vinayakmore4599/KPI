# KPI YAML Operations Guide — Input, Output, and Examples

**Audience:** KPI authors, developers, architecture team  
**Version:** August 2026  
**Related docs:** kpi-yaml-reference.md, kpi_engine/registries/CAPABILITIES.md

---

## 1. Three layers of input

Every measure receives data from three places:

| Layer | What it is | Example |
|---|---|---|
| **A. Host request** | Context JSON — which KPI, measures, filters, anchor month | `kpi_id: 3004`, `measure_key: current_value`, `reporting_month: 2026-07-01` |
| **B. Internal monthly series** | Dense spine for one dimension combo — what the op reads | Table of `event_month` + `sotif_value` for SUPPLIER × NA |
| **C. Other measures** | Scalars from dependency measures (for fn, arithmetic, rank) | `current_value: 150`, `previous_year_value: 90` |

**Output** is always one cell per measure per JSON **row** (one row = one dimension combo × one cut).

---

## 2. Worked scenario (used in all examples below)

**Request context (simplified):**

```json
{
  "execution": {
    "kpi_id": 3004,
    "view_details": [{
      "measures_required": [
        { "measure_key": "current_value" },
        { "measure_key": "previous_year_value" },
        { "measure_key": "value_3m" },
        { "measure_key": "yoy_month" },
        { "measure_key": "trend_12m" },
        { "measure_key": "reason_rank" }
      ]
    }]
  },
  "filters": {
    "reporting_month": { "value": ["2026-07-01"] },
    "region": { "values": ["NA"] }
  }
}
```

**Anchor:** `2026-07-01` (frozen for entire request)

**One dimension combo** (cut R, reason_code=SUPPLIER, region=NA):

**Internal monthly series** (what `point` / `window` / `trend` / `hook` read after densify):

| event_month | sotif_value |
|---|---|
| 2025-05-01 | 70 |
| 2025-06-01 | 80 |
| 2025-07-01 | 90 |
| 2025-08-01 | 85 |
| … | … |
| 2026-05-01 | 100 |
| 2026-06-01 | 120 |
| 2026-07-01 | 150 |

**Base measure YAML:**

```yaml
base_measures:
  sotif_value:
    sql: amount
    agg: sum
```

---

## 3. Output shapes reference

| Op family | JSON cell shape |
|---|---|
| `point`, `fn`, `arithmetic`, `expr` | `{ "value": 150, "period": "2026-07-01", "period_start": "...", "period_end": "..." }` |
| `window` | `{ "value": 370, "period_start": "2026-05-01", "period_end": "2026-07-01" }` |
| `pct_change`, `diff`, `index` | `{ "value": 66.67, "period": "2026-07-01", "baseline_period": "2025-07-01" }` |
| `trend` | `[ {"period":"2025-08-01","value":85}, …, {"period":"2026-07-01","value":150} ]` |
| `rank`, `percent_of_total`, `constant` | bare number: `3` or `0.25` |
| `dimension` | string: `"SUPPLIER"` |
| missing prior period | `null` |

**Full response row example:**

```json
{
  "output_cut": "R",
  "reason_code": "SUPPLIER",
  "region": "NA",
  "grouped_dimensions": ["reason_code", "region"],
  "current_value": {
    "value": 150,
    "period": "2026-07-01",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31"
  },
  "previous_year_value": {
    "value": 90,
    "period": "2025-07-01",
    "period_start": "2025-07-01",
    "period_end": "2025-07-31"
  },
  "value_3m": {
    "value": 370,
    "period_start": "2026-05-01",
    "period_end": "2026-07-01"
  },
  "yoy_month": {
    "value": 66.66666666666667,
    "period": "2026-07-01",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31"
  },
  "reason_rank": 2
}
```

---

## 4. Platform ops — input / YAML / output

### 4.1 `point` — one period value

**Request sends:** `measure_key: current_value`

**Internal input:** monthly series column `sotif_value`; reads row at anchor (or offset anchor)

**YAML:**

```yaml
current_value:
  of: sotif_value
  op: point
  offset: { months: 0 }

previous_year_value:
  of: sotif_value
  op: point
  offset: { years: 1 }
```

**Computation:**

| Measure | Reads month | Raw value |
|---|---|---|
| current_value | 2026-07-01 | 150 |
| previous_year_value | 2025-07-01 | 90 |

**Returns in JSON:**

```json
"current_value": { "value": 150, "period": "2026-07-01" }
"previous_year_value": { "value": 90, "period": "2025-07-01" }
```

If 2025-07 had no data → `"previous_year_value": null`

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base measure name (e.g. `sotif_value`) |
| `offset` | no | `{days\|weeks\|months\|quarters\|years\|periods: N}` — shifts read period **back** from anchor; default 0 |
| `cuts` | no | Limit to cut names, e.g. `[G]`, `[R]` |
| `where` / `also_where` | no | Extra row filter on this measure |
| `ignore_filters` | no | Skip host filters for this measure |
| `required` | no | Host must request this key or bind fails |

---

### 4.2 `window` — aggregate over range

**Request sends:** `measure_key: value_3m`

**Internal input:** same monthly series; sums months in window

**YAML:**

```yaml
value_3m:
  of: sotif_value
  op: window
  trailing: { months: 3 }
  inclusive: true
```

**Computation (anchor Jul 2026, inclusive trailing 3):**

| Month included | Value |
|---|---|
| 2026-05-01 | 100 |
| 2026-06-01 | 120 |
| 2026-07-01 | 150 |
| **Sum** | **370** |

**Returns in JSON:**

```json
"value_3m": {
  "value": 370,
  "period_start": "2026-05-01",
  "period_end": "2026-07-01"
}
```

**QTD example:**

```yaml
value_qtd:
  of: sotif_value
  op: window
  range: qtd
```

**Input:** all months from quarter start through anchor  
**Output:** `{ "value": <sum>, "period_start": "2026-04-01", "period_end": "2026-07-01" }` (Q2 2026)

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base measure name |
| `trailing` | one of trailing/range | `{days\|weeks\|months\|quarters\|years\|periods: N}` or `{from: data_points}` |
| `range` | one of trailing/range | `trailing` (default), `leading`, `mtd`, `qtd`, `ytd`, `wtd`, `cumulative`, `full_month`, `full_quarter`, `full_year` |
| `offset` | no | Shift reference period back before applying range |
| `inclusive` | no | `true` / `false` — include anchor in trailing/leading; default `true` |
| `align` | no | `calendar` (default) or `periods` — how trailing length is counted |
| `cuts` | no | Cut restriction |

Named `range` (`qtd`, `ytd`, …) cannot also set `trailing:`. `leading` requires `trailing:` for length.

---

### 4.3 `trend` — array for charts

**Request sends:** `measure_key: trend_12m`

**Internal input:** last 12 months of `sotif_value` for this combo

**YAML:**

```yaml
trend_12m:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  inclusive: true
  cuts: [G]
```

**Returns in JSON (abbreviated):**

```json
"trend_12m": [
  { "period": "2025-08-01", "value": 85 },
  { "period": "2025-09-01", "value": 88 },
  ...
  { "period": "2026-07-01", "value": 150 }
]
```

**Also in response:**

```json
"trend_axes": {
  "trend_12m": ["2025-08-01", "2025-09-01", ..., "2026-07-01"]
}
```

Missing month in series → slot is `0` for sum/count bases, `null` for others.

**Optional partitioning (`partition_by` / alias `group_by`):**

When the cut grain has multiple dimensions, you can emit one trend **per partition** instead of one series for the whole combo. Names must be catalog dimensions on the cut grain.

```yaml
trend_by_region:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  partition_by: [region]
  cuts: [R]
```

**Input:** monthly series split by `region` within each combo  
**Output:** same as trend — array per row; each row's partition gets its own axis slice

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base measure name |
| `trailing` / `range` | yes | Same as `window` |
| `offset` | no | Shift entire trend axis (e.g. last year's 12 months) |
| `inclusive` | no | Default `true` |
| `partition_by` / `group_by` | no | Catalog dimensions on cut grain — one series per partition |
| `cuts` | no | Usually `[G]` for global sparklines |

---

### 4.4 `trend_arithmetic` — ratio per period

**Request sends:** `measure_key: sotif_pct_trend`

**Internal input:** per-month totals of two bases (not row-level ratios)

**YAML:**

```yaml
base_measures:
  total_po: { sql: total_po, agg: sum }
  supplier_driven_po: { sql: supplier_po, agg: sum }

measures:
  sotif_pct_trend:
    op: trend_arithmetic
    of: [total_po, supplier_driven_po]
    expr: (total_po - supplier_driven_po) / total_po
    trailing: { months: 12 }
    cuts: [G]
```

**Internal input per month:**

| month | total_po | supplier_driven_po | expr result |
|---|---|---|---|
| 2026-05-01 | 1000 | 200 | 0.80 |
| 2026-06-01 | 1100 | 220 | 0.80 |
| 2026-07-01 | 1200 | 180 | 0.85 |

**Returns in JSON:**

```json
"sotif_pct_trend": [
  { "period": "2025-08-01", "value": 0.78 },
  ...
  { "period": "2026-07-01", "value": 0.85 }
]
```

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes (base mode) | `[base_a, base_b, …]` — per-month totals |
| `left` / `right` | alt to `of` | Two measure or base names |
| `fn` | one of fn/expr | Registered measure function over aligned series |
| `expr` | one of fn/expr | Formula with `+ - * /` and measure names |
| `trailing` / `range` / `offset` | base mode | Same as `trend` — inherited in series mode |
| `cuts` | no | Cut restriction |

Series mode: `of:` names existing `trend` measures; parent must **not** set `trailing`/`range`/`offset`.

---

### 4.5 `arithmetic` — combine two measure scalars

**Request sends:** `measure_key: yoy_month`

**Internal input:** evaluated scalars from dependency measures (not raw series)

| Input measure | Value at eval |
|---|---|
| current_value | 150 |
| previous_year_value | 90 |

**YAML:**

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value
  right: previous_year_value
```

**Computation:** `growth_pct(150, 90)` = `(150 - 90) / 90 × 100` = **66.67**

**Returns in JSON:**

```json
"yoy_month": {
  "value": 66.66666666666667,
  "period": "2026-07-01"
}
```

Period comes from request anchor when inputs span different buckets (current vs prior year).

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `fn` | no | Measure function name; default `divide` |
| `of` | alt | `[left_measure, right_measure]` |
| `left` / `right` | alt | Two measure keys (KPI 3004 style) |

Common `fn` values: `divide`, `growth_pct`, `percent`, `subtract`, `sum`, `multiply`, `avg`, `min`, `max`, `attainment`, `ratio_safe`. Full list: `registries/functions/measure.yaml`.

---

### 4.6 `fn` — named function over N measures

**Request sends:** `measure_key: attainment_pct`

**Internal input:** scalars from `inputs` list

| Input | Value |
|---|---|
| actual_value | 150 |
| target_value | 120 |

**YAML:**

```yaml
target_value:
  op: constant
  value: 120

actual_value:
  of: sotif_value
  op: point

attainment_pct:
  op: fn
  fn: attainment
  inputs: [actual_value, target_value]
```

**Computation:** `attainment(150, 120)` = **125.0** (150/120 × 100)

**Returns in JSON:**

```json
"attainment_pct": { "value": 125.0, "period": "2026-07-01" }
```

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `fn` | yes | Registered measure function |
| `inputs` | yes | List of measure keys, or `{param: measure}` map for named params |
| `params` | no | Extra kwargs allowed by the function signature (e.g. `{unit: month}` for `date_add`) |

---

### 4.7 `expr` — formula over measure keys

**Request sends:** `measure_key: fill_rate`

**Internal input:** scalars from measures referenced in expr

| Measure | Value |
|---|---|
| shipped_now | 800 |
| ordered_now | 1000 |

**YAML:**

```yaml
fill_rate:
  op: expr
  expr: shipped_now / ordered_now
```

**Returns in JSON:**

```json
"fill_rate": { "value": 0.8, "period": "2026-07-01" }
```

**Rule:** `expr` identifiers must be **measure keys**, not physical column names.

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `expr` | yes | Formula using measure keys and registered fn calls |
| `inputs` | no | Explicit dependency list when expr names are ambiguous |

---

### 4.8 `constant` — fixed value

**Request sends:** `measure_key: sla_target`

**Internal input:** none (literal)

**YAML:**

```yaml
sla_target:
  op: constant
  value: 95
```

**Returns in JSON:**

```json
"sla_target": 95
```

**Per-dimension map:**

```yaml
region_target:
  op: constant
  by: region
  value: { NA: 90, EU: 85 }
  default: 80
```

**Input:** row's `region` dimension value  
**Output for region=NA:** `"region_target": 90`

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `value` | yes | Single number **or** map `{dim_value: number, …}` |
| `by` | with map | Dimension name for lookup (must be in `dimensions:`) |
| `default` | no | Fallback when row's dimension not in map; may be `null` |

---

### 4.9 `dimension` — echo dimension column

**Request sends:** `measure_key: reason_code`

**Internal input:** dimension value from combo row

**YAML:**

```yaml
reason_code:
  op: dimension
```

**Returns in JSON:**

```json
"reason_code": "SUPPLIER"
```

**Available options:** none — measure key must match a name in `dimensions:`.

---

### 4.10 `compare` — bind-time sugar (YoY in one key)

**Request sends:** `measure_key: yoy`

**YAML (what you write):**

```yaml
yoy:
  op: compare
  of: sotif_value
  mode: yoy
```

**Expands at bind to:** `pct_change` with `offset: { years: 1 }`

**Internal input:** current month 150, prior year month 90

**Returns in JSON:**

```json
"yoy": {
  "value": 66.66666666666667,
  "period": "2026-07-01",
  "baseline_period": "2025-07-01"
}
```

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base measure (bind expands to `pct_change` or `diff`) |
| `mode` | yes | `yoy`, `mom`, `wow`, `qoq`, `pop`, `diff`, `pct_change` |
| `versus` | no | Custom baseline (advanced; see kpi-yaml-reference.md) |

Bind-time only — never evaluated directly. `yoy`/`mom`/`wow`/`qoq` require matching `time.grain`.

---

### 4.11 `filtered_point` — conditional sum at anchor

**Request sends:** `measure_key: closed_amount`

**Internal input:** row-level rows where `status = closed`, summed at anchor month

**Raw detail rows (before fold):**

| event_month | amount | status |
|---|---|---|
| 2026-07-01 | 100 | closed |
| 2026-07-01 | 50 | open |

**YAML:**

```yaml
closed_amount:
  op: filtered_point
  column: amount
  agg: sum
  where: { column: status, op: eq, value: closed }
```

**Returns in JSON:**

```json
"closed_amount": { "value": 100, "period": "2026-07-01" }
```

Only closed rows contribute; open row excluded.

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `column` | one of column/of | Physical column name |
| `of` | one of column/of | Base measure (alternative to `column`) |
| `agg` | yes | `sum`, `count`, `avg`, `min`, `max`, `percentile`, … |
| `where` | yes | Row predicate, e.g. `{column: status, op: eq, value: closed}` |
| `percentile` | with agg | 0–100 when `agg: percentile` |
| `model` | no | Multi-model source alias |

Also accepts `filtered_window` keys: `trailing`, `range`, `inclusive`, `offset`, `cuts`.  
Also accepts `filtered_compare` keys: `mode`, `versus`.

Filter ops: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `like`, `between`, `is_null`, …

---

### 4.12 `hook` — custom series algorithm

**Request sends:** `measure_key: smoothed`

**Internal input:** trailing 12-month series `[100, 110, 105, …, 150]`

**YAML:**

```yaml
smoothed:
  op: hook
  hook: ewma
  of: sotif_value
  trailing: { months: 12 }
```

**Computation:** EWMA weights recent months more → e.g. **142.5** (example)

**Returns in JSON:**

```json
"smoothed": { "value": 142.5, "period": "2026-07-01" }
```

**Hit rate hook:**

```yaml
months_on_sla:
  op: hook
  hook: hit_rate
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

**Internal input:** 12 monthly values + threshold 95  
**Output:** count of months ≥ 95 → e.g. `"months_on_sla": { "value": 9, "period": "2026-07-01" }`

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `hook` | yes | Allowlisted name from `registries/hooks.yaml` |
| `of` | usually | Base measure for series hooks |
| `trailing` / `offset` | hook-dependent | Lookback window or shifted read |
| `value` | some hooks | Threshold bar (`hit_rate`, `forecast_confidence`, …) |
| hook extras | per hook | e.g. `alpha` on `ewma` — see hook registry |

Common hooks: `ewma`, `hit_rate`, `period_max`, `period_min`, `period_avg`, `seasonal_index`, `cagr`, `forecast_confidence`.

---

## 5. Period ops — input / YAML / output

Period ops shift the read period on a base or shiftable measure.

### 5.1 `lag` / `lead`

**YAML:**

```yaml
prior_month:
  op: lag
  of: current_value
  offset: { months: 1 }
```

**Input:** current_value series; reads value at anchor − 1 month  
**Output:** `{ "value": 120, "period": "2026-06-01" }` (June value when anchor is July)

**Partitioning:** `partition_by` on calendar `op: lag` / `lead` is a **bind error**. For entity-level partitions (per customer, per order), use `over: { partition_by: [...] }` on a **base_measure** (Section 8.4).

**Available options (`lag` / `lead`):**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base or shiftable measure (`point`, `window`, `fn`, …) |
| `offset` | yes | Non-zero `{days\|weeks\|months\|quarters\|years\|periods: N}` |

`lag` reads anchor − offset; `lead` reads anchor + offset.

---

### 5.2 `diff` / `pct_change`

**YAML:**

```yaml
yoy:
  op: pct_change
  of: sotif_value
  offset: { years: 1 }
```

**Input:**

| Period | Value |
|---|---|
| 2026-07-01 (current) | 150 |
| 2025-07-01 (offset) | 90 |

**Output:**

```json
"yoy": {
  "value": 66.66666666666667,
  "period": "2026-07-01",
  "baseline_period": "2025-07-01"
}
```

**Available options (`diff` / `pct_change`):**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Base or shiftable measure |
| `offset` | yes | Non-zero calendar or grain offset |

`diff` → current − baseline. `pct_change` → `(current − baseline) / baseline` (same scale as `fn: growth_pct`).

---

### 5.3 `index`

**YAML:**

```yaml
volume_index:
  op: index
  of: sotif_value
  offset: { years: 1 }
```

**Computation:** `150 / 90 × 100` = **166.67**

**Output:**

```json
"volume_index": {
  "value": 166.66666666666667,
  "period": "2026-07-01",
  "baseline_period": "2025-07-01"
}
```

**Available options:** same as `pct_change` — `of` + non-zero `offset`. Computes `current / baseline × 100` (100 = unchanged).

---

### 5.4 `vs_target`

**YAML:**

```yaml
gap:
  op: vs_target
  of: current_value
  vs: sla_target
  as: gap
```

**Input:** current_value=150, sla_target=95  
**Output:** `{ "value": 55, "period": "2026-07-01" }`

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Actual measure or base |
| `vs` | one of vs/value | Target measure key |
| `value` | one of vs/value | Literal target number |
| `as` | no | `gap` (default) = actual − target; `pct` = percent vs target |

---

### 5.5 `threshold`

**YAML:**

```yaml
hit_sla:
  op: threshold
  of: current_value
  cmp: gte
  value: 95
```

**Input:** current_value=150  
**Output:** `"hit_sla": { "value": 1, "period": "2026-07-01" }` (1 = pass, 0 = fail)

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Measure to test |
| `cmp` | no | `gt`, `gte`, `lt`, `lte`, `eq` — default `gte` |
| `value` | one of value/vs | Literal threshold |
| `vs` | one of value/vs | Measure as threshold |

---

## 6. Cut-phase ops — input / YAML / output

Cut ops need **all combos on the cut** before they run.

**Internal input (cut R, 3 suppliers in NA):**

| reason_code | current_value |
|---|---|
| SUPPLIER_A | 150 |
| SUPPLIER_B | 200 |
| SUPPLIER_C | 80 |

---

### 6.1 `rank`

**Without partition (whole cut):**

```yaml
reason_rank:
  op: rank
  of: current_value
  order: desc
```

**Input (all rows on cut R):**

| reason_code | region | current_value |
|---|---|---|
| LATE | NA | 30 |
| LATE | EU | 15 |
| OTHER | NA | 6 |

**Output (rank across entire cut):**

| reason_code | region | reason_rank |
|---|---|---|
| LATE | NA | 1 |
| LATE | EU | 2 |
| OTHER | NA | 3 |

**JSON:** `"reason_rank": 1` (bare integer)

---

**With `partition_by` (rank restarts inside each group):**

```yaml
region_rank_within_reason:
  op: rank
  of: current_value
  partition_by: [reason_code]   # group_by: is an alias
  order: desc
  cuts: [R]
```

**Input:** same table as above

**Output (rank within each reason_code partition):**

| reason_code | region | region_rank_within_reason |
|---|---|---|
| LATE | NA | 1 |
| LATE | EU | 2 |
| OTHER | NA | 1 |

**JSON for LATE/NA row:** `"region_rank_within_reason": 1`

**Rules:**
- Omit `partition_by` → one ranking over **all rows on the cut**
- `partition_by: [dim]` → restart rank inside each distinct `dim` value
- Names must be in `dimensions:` and on the **cut's effective grain**
- If `partition_by` equals the full cut grain, it behaves like omitting it

**Available options (all ranking / share cut ops):**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Measure whose values drive the cut calc |
| `order` | no | `desc` (default) or `asc` — sort direction |
| `partition_by` / `group_by` | no | Reset scope inside sub-groups |
| `order_by` | no | Dimension tie-breakers after `of` sort |
| `versus_cut` | no | Cross-cut denominator (`percent_of_total`, `contribution`) |
| `cuts` | no | Which cuts emit this measure |

---

### 6.2 `percent_of_total`

**Without partition (share of whole cut):**

```yaml
share_of_cut:
  op: percent_of_total
  of: current_value
  cuts: [G]
```

**Input (cut G, two reason codes):**

| reason_code | current_value |
|---|---|
| LATE | 45 |
| OTHER | 6 |

**Computation:** LATE share = `45 / (45+6) × 100` = **88.24**

**JSON:** `"share_of_cut": 88.23529411764706`

---

**With `partition_by` (share within sub-group):**

```yaml
share_within_reason:
  op: percent_of_total
  of: current_value
  partition_by: [reason_code]
  cuts: [R]
```

**Input (cut R — reason × region):**

| reason_code | region | current_value |
|---|---|---|
| LATE | NA | 30 |
| LATE | EU | 15 |
| OTHER | NA | 6 |

**Computation:**
- LATE/NA: `30 / 45 × 100` = **66.67** (denominator = sum within LATE partition only)
- LATE/EU: `15 / 45 × 100` = **33.33**
- OTHER/NA: `6 / 6 × 100` = **100.0**

**JSON:** `"share_within_reason": 66.66666666666667` for LATE/NA row

---

**With `versus_cut` (share vs another cut's total):**

```yaml
share_of_global:
  op: percent_of_total
  of: current_value
  versus_cut: G
  cuts: [R]
```

**Input:** R row value = 30; G cut total for same logical group = 100  
**Output:** `30 / 100 × 100` = **30.0**

Uses the **declared** cut G total as denominator even when G is also emitted via `also_emit`.

**Not** the same as `fn: percent` — that is row-level math between two measures.

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Numerator measure |
| `partition_by` / `group_by` | no | Denominator = sum within partition |
| `versus_cut` | no | Denominator = total on another cut (e.g. `G`) |
| `order_by` | no | Tie-break when ordering matters |
| `cuts` | no | Cut restriction |

---

### 6.3 `ntile`

**YAML:**

```yaml
quartile:
  op: ntile
  of: current_value
  tiles: 4
  order: desc
```

**Output:** bucket 1–4 per row (bare integer)

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Measure to bucket |
| `tiles` | yes | Integer ≥ 2 (e.g. 4 = quartiles) |
| `order` | no | `desc` (default) or `asc` |
| `partition_by` / `group_by` | no | Bucket within partition |
| `order_by` | no | Tie-break dimensions |
| `cuts` | no | Cut restriction |

---

### 6.4 `gap_to_leader`

**YAML:**

```yaml
vs_best:
  op: gap_to_leader
  of: current_value
```

**Input:** max on cut = 200 (SUPPLIER_B)  
**Output for SUPPLIER_A:** `"vs_best": -50` (150 − 200)

**Available options:** `of` (required) + shared cut keys (`order`, `partition_by`, `order_by`, `cuts`). No extra keys — returns `of − partition_max`.

---

### 6.5 `contribution`

**YAML:**

```yaml
yoy_contrib:
  op: contribution
  of: current_value
  vs: previous_year_value
```

**Input:** change per supplier vs total change on cut  
**Output:** each row's share of total YoY movement

**Available options:**

| Key | Required | Values / notes |
|---|---|---|
| `of` | yes | Current-period measure |
| `vs` | yes | Baseline measure (e.g. `previous_year_value`) |
| `partition_by` / `versus_cut` | no | Scope denominator |
| `order`, `order_by`, `cuts` | no | Shared cut keys |

---

## 7. End-to-end data flow diagram

```text
HOST REQUEST                          KPI YAML
─────────────                         ────────
kpi_id: 3004                    →     base_measures.sotif_value
measure_key: current_value      →     measures.current_value { op: point }
reporting_month: 2026-07-01     →     time.filter_code (claimed as anchor)
region: NA                      →     filters → DuckDB WHERE

        │
        ▼
DUCKDB EXTRACT (physical columns, time RANGE, filters)
        │
        ▼
INTERNAL MONTHLY SERIES (one combo)
  2026-05 → 100, 2026-06 → 120, 2026-07 → 150
        │
        ▼
OP EVALUATE (point / window / fn / rank / …)
        │
        ▼
JSON ROW
  { "reason_code":"SUPPLIER", "region":"NA",
    "current_value": { "value":150, "period":"2026-07-01" } }
```

---

## 8. base_measures — input / output

Base measures are **not** returned to the host. They produce internal columns.

### 8.1 sql + agg

**Input:** DuckDB rows with `amount` column  
**YAML:** `{ sql: amount, agg: sum }`  
**Output (internal):** monthly column `sotif_value` per combo × month

**Options:** `sql` (column/expression), `agg` (`sum`, `count`, `avg`, `min`, `max`, `count_distinct`, `percentile`, …), `where`, `model`

### 8.2 Row filter

**Input:** rows with status column  
**YAML:** `{ sql: amount, agg: sum, where: { column: status, op: eq, value: closed } }`  
**Output:** only closed rows contribute to `sotif_value`

**Options:** `where` / nested `and`/`or`/`not`; filter ops: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `like`, `between`, `is_null`, …

### 8.3 Column op

**Input:** `unit_price × quantity` per row  
**YAML:** `{ columns: { price: unit_price, qty: quantity }, op: multiply, agg: sum }`  
**Output:** folded `line_total` column

**Options:** `columns` map, `op` (column function from registry), `agg`, `where`

### 8.4 `over:` with `partition_by` (entity windows)

**When:** Running sum, row number, lag on **order rows** before monthly fold — not calendar `op: lag`.

**Input (raw detail rows):**

| customer_id | order_date | final_amount |
|---|---|---|
| C1 | 2026-05-01 | 100 |
| C1 | 2026-05-15 | 50 |
| C2 | 2026-05-01 | 200 |

**YAML:**

```yaml
base_measures:
  running_final:
    over:
      fn: running_sum
      of: final_amount
      partition_by: [customer_id]
      order_by: [order_date, order_id]
    agg: max
```

**Output (internal):** per customer_id partition, running sum ordered by order_date; folded with `agg: max` to monthly grain.

**Caps:** 500,000 detail rows; 50,000 distinct partition tuples.

**Available `over:` options:**

| Key | Required | Values / notes |
|---|---|---|
| `fn` | yes | `lag`, `lead`, `row_number`, `rank`, `dense_rank`, `running_sum`, `running_avg`, `last_n` |
| `of` | usually | Column for window fns (not needed for `row_number`) |
| `partition_by` | no | Reset window per entity |
| `order_by` | yes | Sort columns within partition |
| `n` | with `last_n` | Number of trailing rows to keep |

Fold with `agg:` (`max`, `sum`, `first`, `last`, …) to monthly grain.

---

## 9. Quick reference — all platform ops

| op | Primary input | Returns |
|---|---|---|
| point | monthly series @ anchor ± offset | `{value, period}` |
| window | series over trailing/PTD range | `{value, period_start, period_end}` |
| trend | series, N months | `[{period,value},…]` |
| trend_arithmetic | two base series per month | `[{period,value},…]` |
| arithmetic | 2 measure scalars | `{value, period}` |
| fn | N measure scalars | `{value, period}` |
| expr | measure scalars in formula | `{value, period}` |
| compare | series (expands to pct_change) | `{value, period, baseline_period}` |
| filtered_* | filtered row detail | same as point/window/trend |
| hook | trailing series | `{value, period}` |
| constant | none / dimension map | bare scalar |
| dimension | combo dimension | bare string |
| lag/lead | shifted series | `{value, period}` |
| diff/pct_change | current vs shifted | `{value, period, baseline_period}` |
| rank | all cut values; optional `partition_by` | bare integer |
| percent_of_total | all cut values; optional `partition_by`, `versus_cut` | bare float 0–100 |
| ntile/zscore/… | all cut values; optional `partition_by` | bare number |
| trend | series; optional `partition_by` | `[{period,value},…]` |
| over: (base) | row detail; `partition_by` + `order_by` | internal column |

Full catalog with YAML templates: Section 10 below and CAPABILITIES.md.

---

## 10. YAML templates (copy-paste)

### point

```yaml
current_value:
  of: sotif_value
  op: point
  offset: { months: 0 }
```

### window trailing

```yaml
value_12m:
  of: sotif_value
  op: window
  trailing: { months: 12 }
  inclusive: true
```

### window QTD

```yaml
value_qtd:
  of: sotif_value
  op: window
  range: qtd
```

### trend

```yaml
trend_12m:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  cuts: [G]
```

### YoY (one key)

```yaml
yoy:
  op: compare
  of: sotif_value
  mode: yoy
```

### YoY (separate keys — KPI 3004 style)

```yaml
current_value:
  of: sotif_value
  op: point
previous_year_value:
  of: sotif_value
  op: point
  offset: { years: 1 }
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value
  right: previous_year_value
```

### rank (whole cut)

```yaml
reason_rank:
  op: rank
  of: current_value
  order: desc
```

### rank (partitioned)

```yaml
region_rank_within_reason:
  op: rank
  of: current_value
  partition_by: [reason_code]
  order: desc
  cuts: [R]
```

### percent_of_total (whole cut)

```yaml
share:
  op: percent_of_total
  of: current_value
```

### percent_of_total (partitioned)

```yaml
share_within_reason:
  op: percent_of_total
  of: current_value
  partition_by: [reason_code]
  cuts: [R]
```

### percent_of_total (vs global cut)

```yaml
share_of_global:
  op: percent_of_total
  of: current_value
  versus_cut: G
  cuts: [R]
```

### over.partition_by on base

```yaml
base_measures:
  running_final:
    over:
      fn: running_sum
      of: final_amount
      partition_by: [customer_id]
      order_by: [order_date, order_id]
    agg: max
```

### hook ewma

```yaml
smoothed:
  op: hook
  hook: ewma
  of: sotif_value
  trailing: { months: 12 }
```

### hook hit_rate

```yaml
months_on_sla:
  op: hook
  hook: hit_rate
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

---

## 11. Partitioning guide — complete reference

Partitioning means **splitting calculation scope** so totals, ranks, or windows reset inside a group. The engine supports four distinct partitioning mechanisms.

### 11.1 Summary — where partitioning is allowed

| Mechanism | YAML location | Key | Resets / scopes |
|---|---|---|---|
| Cut OVER partition | cut-phase measures | `partition_by:` or `group_by:` | Rank, share, ntile, zscore, … within sub-groups on a cut |
| Cross-cut denominator | `percent_of_total` | `versus_cut:` | Denominator from another cut's total |
| Entity row partition | `base_measures.over:` | `partition_by:` | Running sum, row_number, lag on detail rows |
| Trend partition | `op: trend` | `partition_by:` | Trend series scoped to partition dims |
| Post-filter rollup | KPI `having:` | `then_group_by:` | Re-aggregate survivors to coarser grain |
| **NOT allowed** | `op: lag` / `lead` (calendar) | `partition_by:` | **BindError** — use `over:` on base instead |

---

### 11.2 Cut-phase `partition_by` (most common)

Works like SQL `RANK() OVER (PARTITION BY …)` or `SUM() OVER (PARTITION BY …)`.

**Ops that support `partition_by` / `group_by` alias:**

| Op | Partitioning use |
|---|---|
| `rank`, `dense_rank`, `row_number` | Rank within partition |
| `percent_of_total`, `cumulative_share`, `normalize` | Share within partition |
| `ntile`, `percent_rank`, `top_n`, `bottom_n` | Bucket within partition |
| `gap_to_leader`, `gap_to_avg`, `zscore` | Compare to partition max/mean/stdev |
| `running_total`, `running_avg` | Ordered running calc within partition |
| `contribution`, `concentration`, `abc_class`, `pareto_flag` | Analytics within partition |
| `rank_pct_change` | Rank by pct change within partition |

**Bind rules:**
1. Each name in `partition_by` must be a **`dimensions:` catalog name**
2. Each name must appear on the **cut's effective grain** for every cut listed in `cuts:`
3. If request grain changes and drops that dimension → **BindError**

**Example — rank suppliers within each region (cut R):**

```yaml
cuts:
  - name: R
    group_by: [region, supplier]

measures:
  supplier_rank_in_region:
    op: rank
    of: current_value
    partition_by: [region]
    order: desc
    cuts: [R]
```

**Input:**

| region | supplier | current_value |
|---|---|---|
| NA | A | 150 |
| NA | B | 200 |
| EU | C | 80 |
| EU | D | 120 |

**Output:**

| region | supplier | supplier_rank_in_region |
|---|---|---|
| NA | B | 1 |
| NA | A | 2 |
| EU | D | 1 |
| EU | C | 2 |

---

### 11.3 `percent_of_total` partitioning examples

**A. Whole cut (no partition):**

```yaml
share:
  op: percent_of_total
  of: current_value
```

Denominator = sum of `of` on **all rows of this cut**.

**B. Within partition:**

```yaml
share_within_reason:
  op: percent_of_total
  of: current_value
  partition_by: [reason_code]
  cuts: [R]
```

Denominator = sum of `of` among rows sharing the same `reason_code`.

**C. Versus another cut:**

```yaml
regional_share_of_global:
  op: percent_of_total
  of: current_value
  versus_cut: G
  cuts: [R]
```

Denominator = total of `of` on cut **G** (global), applied to each **R** row.

---

### 11.4 `over.partition_by` on base_measures

For **row-level** sequences before monthly aggregation:

```yaml
base_measures:
  order_seq:
    over:
      fn: row_number
      partition_by: [customer_id]
      order_by: [order_date, order_id]
    agg: max

  running_spend:
    over:
      fn: running_sum
      of: amount
      partition_by: [customer_id]
      order_by: [order_date]
    agg: max
```

**Input:** DuckDB detail rows (one row per order)  
**Partition:** resets per `customer_id`  
**Output:** internal base column folded to monthly grain

**Do not confuse with:**

```yaml
# WRONG — bind error on calendar lag
bad_lag:
  op: lag
  of: current_value
  partition_by: [region]
```

---

### 11.5 `partition_by` on `trend`

```yaml
trend_by_site:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  partition_by: [site_category]
  cuts: [R]
```

Emits trend arrays scoped to the partition dimension on the cut grain.

---

### 11.6 `having.then_group_by` (group rollup, not OVER)

After KPI-level `having:` drops groups, optionally re-aggregate survivors:

```yaml
having:
  match: all
  predicates:
    - { of: current_value, cmp: gt, value: 0 }
  then_group_by: [product_category]
```

**Input:** fine-grain combos filtered by predicates  
**Output:** recomputed measures at coarser `then_group_by` grain

---

### 11.7 `order_by` on cut ops (sort within partition)

Optional tie-break / sort order for cut ops:

```yaml
rank_by_date_then_value:
  op: rank
  of: current_value
  partition_by: [region]
  order: desc
  order_by: [launch_date]
  cuts: [R]
```

Sorts by `of` measure first (`order:`), then by dimension columns in `order_by:`.

---

### 11.8 Partitioning mistakes

| Problem | Cause | Fix |
|---|---|---|
| BindError: partition_by not in cut | Dimension not on cut grain | Add to cut `group_by` or change request grain |
| Rank always 1 | Only one row per partition | Check filters; verify data |
| Share sums to >100% across cut | Used `partition_by` when you wanted whole cut | Remove `partition_by` |
| Share wrong on G vs R | Mixed up cut totals | Use `versus_cut: G` explicitly |
| lag partition_by fails | Calendar ops don't partition | Use `over:` on base_measure |

---

## 12. Options reference — complete catalog

Every measure op accepts a **shared platform envelope** plus op-specific keys. Required keys must be present or bind fails.

### 12.1 Shared keys (most measures)

| Key | Applies to | Values / notes |
|---|---|---|
| `op` | all | Registered op name |
| `of` | most | Base measure or measure key |
| `offset` | time ops | `{days\|weeks\|months\|quarters\|years\|periods: N}` |
| `trailing` | window, trend, hooks | Same unit keys, or `{from: data_points}` |
| `range` | window, trend | See §12.2 |
| `inclusive` | trailing/leading | `true` / `false` |
| `cuts` | cut-restricted | `[G, R, …]` |
| `where` / `also_where` | any | Row-level predicate on this measure |
| `ignore_filters` | any | Skip host filters |
| `required` | any | Host must request this key |
| `having` | KPI-level | Post-compute group filter (not on individual ops) |

**Offset units:** calendar keys add together (`{years: 1, months: 2}` = 14 months). `periods:` counts picked grain steps. Do not mix `periods:` with calendar units on one `offset:`.

**Window `range` values:**

| Value | Meaning |
|---|---|
| `trailing` (default) | Last N periods via `trailing:` |
| `leading` | Next N periods; requires `trailing:` for length |
| `mtd`, `qtd`, `ytd`, `wtd` | Period-to-date through anchor |
| `cumulative` | Span start (or year start) through anchor |
| `full_month`, `full_quarter`, `full_year` | Entire calendar period containing anchor |

---

### 12.2 Platform combo ops

| Op | Required | Optional keys | Enum / notes |
|---|---|---|---|
| `point` | `of` | `offset`, `cuts`, `where` | Default offset 0 |
| `window` | `of`, `trailing` **or** `range` | `offset`, `inclusive`, `align`, `cuts` | `align`: `calendar` \| `periods` |
| `trend` | `of`, `trailing`/`range` | `offset`, `inclusive`, `partition_by`, `group_by`, `cuts` | Emits array + `trend_axes` |
| `trend_arithmetic` | `of` or `left`+`right`, `fn` **or** `expr` | `trailing`, `range`, `offset`, `cuts` | Base vs series mode |
| `arithmetic` | `left`+`right` or `of` | `fn` | Default `fn: divide` |
| `fn` | `fn`, `inputs` | `params` | See measure.yaml |
| `expr` | `expr` | `inputs` | Measure keys only |
| `constant` | `value` | `by`, `default` | Map requires `by:` |
| `dimension` | — | — | Key = dimension name |
| `predicate` | `predicates` | `match` | `match`: `all` \| `any`; cmp: `gt`, `gte`, `lt`, `lte`, `eq`, `ne`, `between` |
| `hook` | `hook` | `of`, `trailing`, `offset`, `value`, hook extras | Per `hooks.yaml` |
| `compare` | `of`, `mode` | `versus` | Bind sugar → `pct_change`/`diff` |
| `filtered_point` | `column`/`of`, `agg`, `where` | `percentile`, `model`, `offset`, `cuts` | Bind sugar → masked `point` |
| `filtered_window` | above + window keys | `trailing`, `range`, `inclusive`, `offset` | Bind sugar → masked `window` |
| `filtered_trend` | above + trend keys | `trailing`, `range`, `inclusive`, `cuts` | Bind sugar → masked `trend` |
| `filtered_compare` | above + `mode` | `versus` | Bind sugar → masked compare |

---

### 12.3 Period ops

| Op | Required | Optional | Enum / notes |
|---|---|---|---|
| `lag` / `lead` | `of`, `offset` (non-zero) | `cuts` | Cannot `partition_by` |
| `diff` / `pct_change` | `of`, `offset` | `cuts` | Shifts baseline selection |
| `index` | `of`, `offset` | `cuts` | `current/baseline×100` |
| `vs_target` | `of`, `vs` **or** `value` | `as` | `as`: `gap` \| `pct` |
| `threshold` | `of`, `value` **or** `vs` | `cmp` | `cmp`: `gt`, `gte`, `lt`, `lte`, `eq` |
| `annualize` | `of` | `n`, `periods_per_year` | Scales to yearly rate |
| `vs_prior_window` | `of`, `offset`, `trailing`/`range` | `inclusive` | Window YoY-style compare |
| `delta_contribution` | `of`, `offset` **or** `vs` | `cuts` | Absolute delta, not share |
| `baseline_index` | `of` | `vs`, `offset` | `of/vs×100` |
| `compound_growth` | `of`, `n` | — | Fixed N-period CAGR formula |
| `seasonal_adjust` | `of` | `trailing` | Deseasonalize vs same-month mean |

---

### 12.4 Combo add-on ops

| Op | Required | Optional | Enum / notes |
|---|---|---|---|
| `expanding_window` | `of` | `range`, `cuts` | Default `range: cumulative` |
| `shifted_trend` | `of`, `offset`, `trailing`/`range` | `inclusive`, `cuts` | Trend on shifted axis |
| `rate` | `of` | `vs`, `n`, `trailing` | `of/vs` or `of/n` |
| `cumulative_point` | `of` | `range`, `cuts` | PTD at anchor |
| `n_period_avg` | `of`, `trailing`/`range` | `inclusive`, `offset` | Mean of period values |
| `weighted_window` | `of`, `weight`, `trailing`/`range` | `align`, `inclusive`, `offset` | Weighted trailing sum |
| `snapshot_compare` | `of`, `vs` | `mode` | `mode`: `pct` \| `diff` — no offset |
| `band` / `envelope` | `of`, `low`, `high` | `method`, `emit`, `side`, `vs` | `method`: `factor` \| `offset`; expands to `_low`/`_high` |

---

### 12.5 Cut-phase ops

**Shared cut keys** (all cut ops): `of`, `order` (`asc`|`desc`), `partition_by`/`group_by`, `order_by`, `versus_cut`, `cuts`.

| Op | Extra required | Extra optional | Output |
|---|---|---|---|
| `rank` | — | shared | Integer rank (ties share rank, gaps) |
| `dense_rank` | — | shared | Integer rank (no gaps) |
| `row_number` | — | shared | Unique 1..n |
| `percent_of_total` | — | `versus_cut` | Float 0–100 share |
| `percent_rank` | — | shared | Float 0–100 rank percentile |
| `ntile` | `tiles` (≥2) | shared | Integer bucket 1..N |
| `top_n` / `bottom_n` | `n` (≥1) | shared | 1/0 flag |
| `cumulative_share` | — | shared | Running share 0–100 (Pareto) |
| `running_total` / `running_avg` | — | shared | Ordered running calc |
| `gap_to_leader` / `gap_to_avg` | — | shared | Float delta vs max/mean |
| `zscore` | — | shared | Standard score |
| `contribution` | `vs` | `versus_cut` | Share of `(of−vs)` change |
| `rank_pct_change` | — | `offset` (default `years: 1`) | Rank by period % change |
| `concentration` | — | shared | HHI 0–1 (same on all rows in partition) |
| `abc_class` | — | `a_share` (80), `b_share` (95) | String `A`/`B`/`C` |
| `pareto_flag` | — | `share` (80) | 1/0 inside leading band |
| `normalize` | — | `method` | `method`: `max` \| `sum` |

---

### 12.6 `compare` modes

| `mode` | Expands to | Default offset | Grain check |
|---|---|---|---|
| `yoy` | `pct_change` | `{years: 1}` | Must allow year grain |
| `mom` | `pct_change` | `{months: 1}` | Must allow month |
| `wow` | `pct_change` | `{weeks: 1}` | Must allow week |
| `qoq` | `pct_change` | `{quarters: 1}` | Must allow quarter |
| `pop` | `pct_change` | `{periods: 1}` | Uses picked grain |
| `diff` | `diff` | `{periods: 1}` | Absolute change |
| `pct_change` | `pct_change` | `{periods: 1}` | Percent change |

---

### 12.7 `base_measures` options

| Pattern | Keys | Notes |
|---|---|---|
| SQL fold | `sql`, `agg` | `agg`: `sum`, `count`, `avg`, `min`, `max`, `count_distinct`, `percentile`, … |
| Row filter | `where` | Same filter ops as measures |
| Column op | `columns`, `op`, `agg` | Column function from registry |
| Row helper | `expr` / `lookup` / `over` (no `agg`) | Needs `identity_grain:` |
| Entity window | `over: {fn, of, partition_by, order_by}` | `fn`: see §8.4 |
| Static map | `lookup: {map, default, valid_from, valid_to}` | Effective-dated optional |

---

### 12.8 Registered measure functions (common)

Use with `op: fn` or `op: arithmetic`:

| Category | Names |
|---|---|
| Math | `divide`, `sum`, `subtract`, `multiply`, `avg`, `min`, `max`, `abs`, `round`, `floor`, `ceil`, `power`, `log`, `log10`, `sqrt` |
| Change | `growth_pct`, `percent`, `bps_change`, `log_change`, `attainment`, `ratio_safe` |
| Logic | `coalesce`, `if_null`, `nullif`, `null_if_zero`, `zero_if_null`, `is_null`, `is_not_null`, `if_else`, `if_between`, `clamp` |
| Stats | `geomean_scalars`, `harmonic_mean`, `weighted_avg_scalars`, `min_max_spread` |
| Labels | `sign_label` |
| Dates | `date_diff`, `date_add`, `epoch_day` |

Full list with arity: `kpi_engine/registries/functions/measure.yaml` and CAPABILITIES.md.

---

## 13. Common mistakes

| Problem | Cause | Fix |
|---|---|---|
| Always null for prior year | No data at offset month | Check extract span / densify |
| YoY looks wrong | Averaged row ratios in base | Use two sum bases + expr |
| rank always 1 | Only one combo on cut | Check filters / grain |
| trend empty | Wrong cut (`cuts: [G]` only) | Add cut or remove `cuts:` |
| `{value, period}` missing | Cut-phase op | rank returns bare integer |
| bind error on `year: 1` | Wrong offset key | Use `years: 1` |
| partition_by bind error | Dim not on cut grain | See Section 11.2 rules |

---

## 14. Further reading

- kpi-yaml-reference.md — full key reference
- docs/KPI-Engine-Pipeline-Deep-Dive.docx — how data flows through pipeline
- kpi_config/kpis/sotif/3004.yaml — gold KPI example
