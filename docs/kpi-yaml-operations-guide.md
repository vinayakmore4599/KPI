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

**YAML:**

```yaml
reason_rank:
  op: rank
  of: current_value
  order: desc
```

**Output per row:**

| reason_code | current_value | reason_rank |
|---|---|---|
| SUPPLIER_B | 200 | 1 |
| SUPPLIER_A | 150 | 2 |
| SUPPLIER_C | 80 | 3 |

**JSON:** `"reason_rank": 2` (bare integer, no period wrapper)

---

### 6.2 `percent_of_total`

**YAML:**

```yaml
share:
  op: percent_of_total
  of: current_value
```

**Computation:** SUPPLIER_A share = `150 / (150+200+80)` = **0.349**

**JSON:** `"share": 0.3488372093023256`

**Not** the same as `fn: percent` — that is row-level math between two measures.

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

### 8.2 Row filter

**Input:** rows with status column  
**YAML:** `{ sql: amount, agg: sum, where: { column: status, op: eq, value: closed } }`  
**Output:** only closed rows contribute to `sotif_value`

### 8.3 Column op

**Input:** `unit_price × quantity` per row  
**YAML:** `{ columns: { price: unit_price, qty: quantity }, op: multiply, agg: sum }`  
**Output:** folded `line_total` column

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
| rank | all cut values | bare integer |
| percent_of_total | all cut values | bare float 0–1 |
| ntile/zscore/… | all cut values | bare number |

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

### rank

```yaml
reason_rank:
  op: rank
  of: current_value
  order: desc
```

### percent_of_total

```yaml
share:
  op: percent_of_total
  of: current_value
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

## 11. Common mistakes

| Problem | Cause | Fix |
|---|---|---|
| Always null for prior year | No data at offset month | Check extract span / densify |
| YoY looks wrong | Averaged row ratios in base | Use two sum bases + expr |
| rank always 1 | Only one combo on cut | Check filters / grain |
| trend empty | Wrong cut (`cuts: [G]` only) | Add cut or remove `cuts:` |
| `{value, period}` missing | Cut-phase op | rank returns bare integer |
| bind error on `year: 1` | Wrong offset key | Use `years: 1` |

---

## 12. Further reading

- kpi-yaml-reference.md — full key reference
- docs/KPI-Engine-Pipeline-Deep-Dive.docx — how data flows through pipeline
- kpi_config/kpis/sotif/3004.yaml — gold KPI example
