# KPI Calculation Framework — Final Plan

This document is the working architecture for a reusable, config-driven KPI engine. It consumes a context JSON produced by an existing metadata framework, loads data with DuckDB, calculates in Pandas, and returns JSON.

It supersedes earlier drafts. Decisions recorded here are locked unless explicitly marked as a recommendation.

Authoring docs that follow this architecture: [README.md](README.md), [kpi-onboarding-guide.md](kpi-onboarding-guide.md), [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md), [kpi-yaml-reference.md](kpi-yaml-reference.md), and the live name list [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md).

---

## 1. Purpose and boundary

### 1.1 What this framework does

- Consume a request context (filters, datasets, requested measures).
- Resolve a KPI definition from YAML.
- Extract data from ADLS (Delta/Parquet) via DuckDB.
- Compute requested measures (point, window, arithmetic, trend).
- Return a JSON payload the caller can render as tables and graphs.

### 1.2 What this framework does not own

These stay in the existing platform:

- Context construction from metadata tables.
- ADLS credentials and access.
- Authentication and authorization.
- Databricks jobs (not used).
- Hierarchy expansion for `heir` filters (recommendation: keep upstream).

The engine is a library invoked from an existing UDF entry point. It is not a standalone service.

### 1.3 Product goal

Onboard a new KPI with YAML. A new reusable name (op, hook, function) is `capabilities/` + `registries/`, not `core/`. No context-schema changes, no ADLS plumbing.

---

## 2. Locked decisions

| Area | Decision |
|---|---|
| Host | In-process Python library. Entry point for now: `udfs.sotif.main` as a thin shim over a generic engine. |
| Context | Immutable envelope. Adapter maps it; we do not redesign it. |
| Paths | Taken from `context.datasets`. KPI/model YAML does not own file locations. |
| `business_date` | Present on the context. **Never used in calculations.** |
| Model | Physical YAML (tables + joins) **or** a SQL/CTE query. Both run in DuckDB. |
| Split | DuckDB: scan, IN filters, column prune, **base GROUP BY** to monthly grain. Pandas: all KPI calculations. |
| Measure language | SQL column expressions + declared `agg` in YAML. No Python `eval`. No `df['x'].sum()` strings. |
| Anchor | User-selected month from filters. Frozen once per request. Identical for every cut and measure. |
| Month filter | Claimed by the time planner. **Must never become a generic `IN` on a single month.** Pushed down as a date **range**. |
| Lookback | `required_span` = deepest lookback among **requested** measures. Widen the scan, calculate, then collapse. Unrequested measures do not widen the scan. |
| Shifts | Calendar arithmetic on a dense month spine. 3 / 6 / 12 months move by months, not by rows. |
| Output | JSON. One row per dimension combination per cut. One column per requested `measure_key`. |
| Scalars | Point, window, and arithmetic measures are single numbers at the selected month. |
| Trends | Authored `measure_key` whose value is an ordered array of monthly points (for graphs). |
| Measures | Every `measure_key` is authored in KPI YAML. Nothing is inferred from catalog op ids. |
| Catalog | YAML registries under `registries/` (ops, hooks, column fns, measure fns). Live page: `registries/CAPABILITIES.md`. Platform kinds (`point`, `window`, `trend`, `arithmetic`, `fn`, `expr`, `constant`, `dimension`, `hook`, `rank`, `percent_of_total`) plus allowlisted add-ons. |
| Freeze | A new **name** does not edit `core/`, `extensions/`, `contracts.py`, or a hardcoded name list in tests. A new **agg**, filter operator, compose template, time format, or *common* measure field (`offset`-like) is engine work. |
| Cuts | Generic. G and R are examples. YAML: `group_by`, `ignore_filters`, `also_emit`. |
| Level G (example) | Ignore region filter. Compute global (no region in group by) **and** regional (region in group by) from the same extract. |
| Additivity | Declared per measure (`sum`, `avg`, `count`, `window`, …). Global is recomputed with that agg, never rolled up from regional rows for avg/window. |
| Multi-model | Join keys declared in KPI YAML (`model_relations`). |
| Filters | Default operator **IN**. Scalar becomes a one-element list. Accept both `value` and `values`. |
| Filter stage | YAML `filters.*.apply`: `extract` (DuckDB WHERE), `calc` (Pandas before measures; required when a cut `ignore_filters` that code), `result` (drop JSON rows after measures). Undeclared host `IN` lists default to extract unless a cut ignores them. Unbindable filter → **hard error**. |
| Scope | One KPI, one view. All measures for that KPI live in that view. Assert `view_details` has exactly one entry. |
| Custom logic | Named hook allowlisted in `registries/hooks.yaml`, body under `capabilities/hooks/`. Not dotted `importlib` paths from YAML. `extensions/` is a shim only. |
| Pagination | After all calculation. Deterministic sort. Return `total_count` / `has_more`. Null `page_size` means return all rows. |

---

## 3. Runtime path

```text
context JSON ─┐
              ├─► Adapter ─► Binder ─► Time planner ─► DuckDB extract
KPI YAML    ─┘                              │              │
Registries  ────────────────────────────────┤              │
                                            ▼              ▼
                                      required_span    monthly grain
                                            │              │
                                            └──────► Pandas
                                                       │
                                                       ├ dense month spine
                                                       ├ cuts (re-agg / tag)
                                                       ├ registered plugins
                                                       ├ project measure_keys
                                                       ├ sort / paginate
                                                       └ JSON
```

### 3.1 Steps

1. **Parse context.** Assert one view. Normalize `value` / `values` to lists. Echo `request_id`.
2. **Load KPI YAML** by `execution.kpi_id` (e.g. `kpis/3004.yaml`).
3. **Bind datasets.** Map model aliases to `context.datasets` (bind on `alias`, fall back to datasets key).
4. **Claim the month filter.** Remove it from the generic filter list. Its value is the **anchor**.
5. **Compute `required_span`.** Anchor minus the largest lookback among requested measures.
6. **Split remaining filters.** Source vs target using `filter_column_mappings` (and optional YAML `filter_map`).
7. **DuckDB extract.** Run the physical model or wrap the SQL model as a subquery. Apply source `IN` filters and the time **range**. Project needed columns. **GROUP BY** to the finest grain any cut needs (time grain + union of cut keys) for additive measures.
8. **Pandas spine.** Reindex each partition onto a dense calendar at `time_grain` from span min to anchor.
9. **Cuts.** For each emitted cut, aggregate with that cut’s `group_by`. Drop ignored filters before aggregation. Tag `output_cut`.
10. **Registered plugins.** Each requested measure runs its `OpPlugin` (combo or cut phase) from the monthly frame. Names come from `registries/ops.yaml` / `hooks.yaml`.
11. **Project.** Keep only requested `measure_key` columns (plus dimension columns and `output_cut`).
12. **Sort, paginate, emit JSON.** Include metadata: anchor, effective period, applied/ignored filters, period axes for trends, warnings.

### 3.2 DuckDB vs Pandas (why both)

The model file answers “what did we query?” The YAML registries answer “what names can we compute?”

DuckDB must still do the **base GROUP BY**. A widened lookback of several years at row grain will not fit in Pandas. After that handoff, Pandas owns every derived, time, cut, and cross-model calculation.

Pre-aggregation eligibility:

| `agg` | Shared DuckDB GROUP BY | Notes |
|---|---|---|
| `sum`, `count`, `min`, `max` | Safe | Re-aggregate per cut in Pandas |
| `avg` | Safe if carried as **sum and count** | Divide after re-aggregation; never average of averages |
| `count_distinct`, `median`, `percentile` | Not safe | One DuckDB aggregation **per cut** |

---

## 4. Context adapter

The engine does not change the context schema. It maps the existing envelope.

### 4.1 Sample (illustrative)

```json
{
  "execution": {
    "source": "metadata",
    "request_id": "REQ-page-001",
    "kpi_id": 3004,
    "view_details": [
      {
        "view_id": 13,
        "view_name": "Sotif",
        "measures_required": [
          { "measure_key": "previous_year_value" },
          { "measure_key": "reason_code" }
        ]
      }
    ],
    "user_id": "id",
    "business_date": null
  },
  "filters": {
    "Supplier Name": { "value": ["ABC"], "input_text": "heir" },
    "region": { "values": ["NA"], "input_text": "simple" }
  },
  "datasets": {
    "Sotif": {
      "dataset_id": 21,
      "dataset_name": "CDL_SOTIF",
      "table_type": "DELTA",
      "path": "abfss://...",
      "container_name": "command",
      "partition_key": null,
      "alias": "sotif",
      "columns": ["A", "b", "c"],
      "filter_column_mappings": [
        {
          "filter_id": 67,
          "filter_code": "region",
          "view_id": 13,
          "column_name": "region",
          "operator": "in"
        }
      ],
      "join_type": null,
      "join_condition": null,
      "join_managed_by": "udf"
    }
  },
  "udf": {
    "udf_id": 6,
    "udf_name": "sotif",
    "udf_type": "MEASURE",
    "module_path": "udfs.sotif,main",
    "output_type": "df"
  },
  "output": {
    "response_type": "pagination",
    "page": null,
    "page_size": null,
    "limit": null
  }
}
```

### 4.2 Field map

| Context path | Engine use |
|---|---|
| `execution.kpi_id` | Load `config/kpis/<kpi_id>.yaml` |
| `execution.request_id` | Echo in result metadata |
| `execution.view_details` | Assert length = 1. Read `measures_required[].measure_key` |
| `execution.business_date` | Ignore for all calculations |
| `filters.*` | IN lists. Accept `value` or `values` |
| `filters.*.input_text` | `simple` = use the list. `heir` = see hierarchy recommendation |
| Month / period filter (name TBD) | **Time planner**, not generic IN |
| `datasets.*.path`, `table_type`, `alias` | DuckDB scan target |
| `datasets.*.columns` | Projection and bind-time column check |
| `datasets.*.filter_column_mappings` | `filter_code` → `column_name`, operator (already `in`) |
| `datasets.*.join_managed_by: udf` | Legacy. Ignored when a model YAML/SQL exists |
| `udf.module_path` | Current entry shim only. YAML KPIs do not import this path |
| `output.page` / `page_size` / `limit` | Paginate the **result**, never the extract |

### 4.3 Adapter rules

- Match filters to mappings by `filter_code` first, then case-insensitive filter key.
- Normalize operator to IN (default).
- Empty IN list → match nothing (empty result), not invalid SQL.
- Unmapped filter that cannot bind to a column → hard error, listing the filter name.
- `view_details` with 0 or 2+ entries → hard error.
- Unknown `measure_key` → hard error, listing valid keys from the KPI YAML `measures` block.

### 4.4 Month filter (critical)

The agreed generic rule is: bind filter → `column IN (...)`.

**That rule must not apply to the selected month.** `WHERE month IN ('2026-08')` discards the history that 3m / 6m / 12m / previous year / trends need. Those measures would return null while other filters still worked, which looks like missing data.

Correct handling:

1. Classify the month filter and remove it from the generic list **before** binding.
2. Its value becomes the request **anchor**.
3. `required_span` = `[anchor - max_lookback, anchor]`.
4. DuckDB gets a range predicate on `time_column`, not IN on one month.
5. After calculation, collapse to scalars / trend arrays at the anchor. The widened scan is not visible to the caller.

**If the month filter is missing:** hard error naming the required filter. Do not default to “latest data” or “today”; that reintroduces drift.

**Format:** Normalize `2026-08` and `2026-08-01` to the same `DATE` bucket (first day of the period) before any arithmetic.

Which filter key carries the month must be confirmed with the metadata team and wired as a named claim in the adapter (and optionally in KPI YAML as `time.filter_code`).

---

## 5. Models (DuckDB)

A model is the extract. It is not the KPI.

### 5.1 Physical model (`kind: physical`)

YAML describes logical sources and joins. Runtime paths come from context.

```yaml
# config/models/sotif.yaml
model_id: sotif
kind: physical
required_aliases: [sotif]

sources:
  sotif:
    alias: sotif
    # path, format, columns come from context.datasets by alias

# Optional extra joins if the context does not already describe them
joins: []
```

Engine builds:

```sql
SELECT <projected columns>
FROM delta_scan('<context path>') AS sotif
WHERE <source IN filters>
  AND <time_column> >= <span_start>
  AND <time_column> <  <span_end_exclusive>
GROUP BY <finest grain>
```

No `SELECT *`. Project only columns required by measures, grains, filters, and joins.

### 5.2 SQL model (`kind: sql`)

Use when source logic is CTEs, SCD, eligibility, or otherwise too messy for join YAML.

```yaml
# config/models/sotif_sql.yaml
model_id: sotif_sql
kind: sql
required_aliases: [sotif]
output_schema:
  - { name: order_month, type: date }
  - { name: region, type: varchar }
  - { name: reason_code, type: varchar }
  - { name: amount, type: decimal }
sql: |
  WITH base AS (
    SELECT ...
    FROM delta_scan($sotif_path)
  )
  SELECT * FROM base
```

Engine wraps the SQL as a subquery, substitutes dataset paths as parameters (never string-built from user input), then applies the same IN filters and time range on **declared** `output_schema` columns.

**Recommendation:** verify `output_schema` in CI with `SELECT * FROM (<sql>) WHERE 1=0` (schema only, no rows).

### 5.3 Binding datasets

Recommendation: bind on `alias`, fall back to the `datasets` object key. The model lists `required_aliases`. Mismatch is a precise error (“model wants `sotif`, context has …” ), not a missing-column failure inside SQL.

Confirm with metadata that `alias` is stable across environments (`Sotif` vs `CDL_SOTIF` vs `sotif`).

---

## 6. KPI YAML

One file per `kpi_id`. This is the onboarding surface.

### 6.1 Shape

```yaml
# config/kpis/3004.yaml
kpi_id: 3004
version: 1
model: sotif

time:
  column: event_month          # source column, truncated to DATE
  grain: month                 # day | month | quarter | year
  filter_code: reporting_month # claimed by time planner (confirm actual name)
  calendar: gregorian

dimensions:
  - { name: reason_code, kind: dimension }
  - { name: region, kind: dimension }

base_measures:
  sotif_value:
    sql: amount                # SQL column expression, not a SELECT
    agg: sum                   # sum | avg | count | count_distinct | min | max

cuts:
  - name: G
    group_by: [event_month]    # time grain always implied at extract; collapsed later
    ignore_filters: [region]
    also_emit: [R]
  - name: R
    group_by: [event_month, region]
    ignore_filters: []

default_cut: G                 # used when context has no cut/level filter

measures:
  reason_code:
    kind: dimension

  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }

  previous_year_value:
    of: sotif_value
    op: point
    offset: { years: 1 }

  value_3m:
    of: sotif_value
    op: window
    trailing: { months: 3 }
    inclusive: true

  value_6m:
    of: sotif_value
    op: window
    trailing: { months: 6 }
    inclusive: true

  value_12m:
    of: sotif_value
    op: window
    trailing: { months: 12 }
    inclusive: true

  yoy_month:
    op: arithmetic
    fn: growth_pct
    left: current_value
    right: previous_year_value

  trend_12m:
    of: sotif_value
    op: trend
    trailing: { months: 12 }
    inclusive: true
    cuts: [G]                  # which cuts carry the array; default G
```

`measures_required` in context is a **projection**: only those `measures` keys (plus required dimensions / `output_cut`) appear in the JSON.

### 6.2 Multi-model relation

When two measures use different models, declare the join on **aggregated** outputs:

```yaml
model_relations:
  - left: sotif_value
    right: marketing_spend
    on: [event_month, region]  # region omitted automatically on G-only frames
    how: outer                 # default outer so sparse keys do not drop KPIs
```

### 6.3 Cuts (G / R and future levels)

G and R are examples, not engine features.

| Field | Meaning |
|---|---|
| `name` | Value written to `output_cut` |
| `group_by` | Dimensions for that aggregation (time is handled by the monthly frame) |
| `ignore_filters` | Dropped before aggregation for this cut (e.g. region when computing G) |
| `also_emit` | Extra cuts packed into the same response |

**Correctness:** ignored filters are dropped on the extract used for those cuts. Each emitted cut is a **fresh aggregation** from the monthly frame using that cut’s `agg`. Do not roll child cuts into a parent unless the measure is additive **and** the KPI explicitly opts into rollup.

When `level` / cut is not in context, use `default_cut` + `also_emit`.

Result: dimensions not in a cut’s `group_by` are `null` on those rows.

---

## 7. Calculation catalog

Reusable names. KPI YAML names them; authors do not reimplement YoY or trailing windows.

The live list is [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md), generated from four YAML files: `registries/ops.yaml`, `registries/hooks.yaml`, `registries/functions/column.yaml`, `registries/functions/measure.yaml`. Bodies live under `capabilities/`. `core/` loads those files; it does not contain a name list.

| Layer | Examples | Where it lives |
|---|---|---|
| Point / window / trend | `current_value`, `value_3m`, `trend_12m` | Platform ops (`capabilities/ops/combo.py`) |
| Arithmetic / fn / expr / constant | YoY, ratio, nested `+ - * /`, a target | Platform ops + function registries |
| Cut-wide | `rank`, `percent_of_total`, add-ons `ntile`, `top_n`, `gap_to_leader`, … | `capabilities/ops/cut.py` |
| Period | add-ons `lag`, `lead`, `diff`, `pct_change`, `index`, `vs_target` | `capabilities/ops/period.py` |
| Hook | `ewma`, `hit_rate`, `cagr`, … | `capabilities/hooks/` + `registries/hooks.yaml` |

A new **name** = impl + registry row + regenerate `CAPABILITIES.md`. A new **agg**, filter operator, compose template, time format, or common measure field = `core/` (and `contracts.py` if the field is shared).

### 7.1 Composition order (locked recommendation)

Stages, one direction only:

0. Row level in DuckDB (extract + base agg).
1. Base aggregate per cut (already monthly).
2. Combo-phase plugins (point / window / trend / arithmetic / fn / expr / hook / period add-ons).
3. Cut-phase plugins (`rank`, `percent_of_total`, `ntile`, …) on that cut's rows.
4. Arithmetic / fn / expr on combo results (e.g. YoY of two points). Cut-phase results cannot feed those in the same request.

**Invariant:** no aggregation may consume a computed ratio. Ratios are ratio-of-sums, never sum-of-ratios, unless an op is explicitly row-level.

Each op declares input kind (`aggregate` | `point` | `window` | `trend`) and output kind. Illegal wiring fails at validate time.

YoY is not a single meaning. Author distinct keys:

- `yoy_month` — selected month vs same month last year.
- `yoy_12m` — trailing 12 months vs the 12 months before that.

### 7.2 `required_span`

Each output declares its lookback. The planner takes the **max among requested keys only**.

| Requested | Span (inclusive of anchor, month grain) |
|---|---|
| `value_3m` only | 3 periods |
| `value_3m` + `previous_year_value` | 13 periods |
| `trend_12m` | 12 periods |
| `trend_12m` + `previous_year_value` | 13 periods |
| Trend of monthly YoY (12 points) | 24 periods |

### 7.3 Dense month spine

Required for **point** ops. `shift(12)` on rows is wrong when a partition misses a month.

- Reindex each partition (cut keys except time) onto every month in `[span_start, anchor]`.
- Fill policy follows the measure: sums/counts fill `0`; averages, ratios, and point lookups stay `null`.

Window aggregates filter by date range and do not need the spine for correctness, but one spine for all ops is simpler.

### 7.4 Window inclusivity (recommendation)

Declare per op; **default inclusive of the anchor**.

| Convention | 3 months at March 2026 |
|---|---|
| Inclusive (default) | Jan, Feb, Mar 2026 |
| Exclusive | Dec 2025, Jan, Feb 2026 |

The same flag decides whether a 12-month trend ending at March includes March.

---

## 8. JSON output contract

### 8.1 Row grain

- One row per **dimension combination** per **cut**.
- Time is **not** a row axis for scalar measures.
- Each requested `measure_key` is a column.

Dimensions: from the cut’s `group_by` plus any requested dimension keys (null if not in that cut).

### 8.2 Scalar columns

Point, window, and arithmetic → JSON number or `null`.

### 8.3 Trend columns

Trend `measure_key` → ordered array of numbers/`null`, aligned to a **shared period axis** in metadata (not `{period, value}` pairs on every row).

```json
{
  "kpi_id": 3004,
  "request_id": "REQ-page-001",
  "parameters": {
    "anchor": "2026-03-01",
    "time_grain": "month",
    "inclusive": true
  },
  "applied_filters": [
    { "filter_code": "region", "column": "region", "op": "in", "values": ["NA"], "stage": "extract" }
  ],
  "ignored_filters": [
    { "filter_code": "region", "reason": "cut_G_ignore_filters" }
  ],
  "trend_axes": {
    "trend_12m": ["2025-04-01", "2025-05-01", "2025-06-01", "2025-07-01", "2025-08-01", "2025-09-01", "2025-10-01", "2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01"]
  },
  "pagination": {
    "page": null,
    "page_size": null,
    "total_count": 2,
    "has_more": false
  },
  "rows": [
    {
      "output_cut": "G",
      "region": null,
      "reason_code": "LATE_SUPPLIER",
      "value_3m": 1204,
      "previous_year_value": 402,
      "trend_12m": [null, 80, 90, 100, 110, 95, 88, 102, 97, 91, 85, 70]
    },
    {
      "output_cut": "R",
      "region": "NA",
      "reason_code": "LATE_SUPPLIER",
      "value_3m": 706,
      "previous_year_value": 245
    }
  ]
}
```

Trend arrays are omitted on cuts not listed in `measures.*.cuts` (default: global / `G` only). Do not send a null array on every supplier row unless the KPI asks for that.

**Missing months in a trend:** the array always has a slot for every period on the axis. A gap must not shorten the array (that would shift the graph). Fill: zero vs null per measure policy.

**Payload guard:** fail clearly if `row_count × trend_length` exceeds a configured cap, naming the trend key and cut.

### 8.4 Which dimension rows appear (recommendation)

Build the row set from the **union of dimension combinations across `required_span`**, not only combinations present in the anchor month.

If you only emit combinations present at the anchor, a supplier with 12 months of history but nothing in the selected month disappears from regional rows while still contributing to the global window total. Regional rows then **do not sum to global** — a reconciliation failure that is hard to explain.

Opt-out flag allowed for KPIs that truly want “active this month only.”

### 8.5 Pagination

- After calculation (rolling/trend need the full series internally).
- Sort: `output_cut`, then dimension keys (stable, documented).
- `page_size: null` → return all rows.
- Expose `output_cut` so a UI can page one cut.

---

## 9. Filters

### 9.1 Default operator

**IN.** `"APAC"` → `IN ('APAC')`. Lists stay lists.

### 9.2 Extract / calc / result

| `apply` | Stage |
|---|---|
| `extract` (default for host `IN` lists) | DuckDB `WHERE` on the retrieve |
| `calc` | Pandas mask before measures. Required when a cut lists the code in `ignore_filters` |
| `result` | Drop JSON rows after measures (shares still use the unfiltered cut) |
| Cannot bind | Hard error |

`ignore_filters` is not valid with `apply: extract` or `apply: result`. Filter-to-column mapping is primarily `datasets.*.filter_column_mappings`. Optional YAML `filter_map` for KPI-specific aliases.

### 9.3 Hierarchy (`input_text: "heir"`)

Recommendation: expand to leaf values **in the context builder** so this engine only ever sees a flat IN list.

Until that exists: **reject** `heir` filters explicitly. Do not treat them as `simple`.

Fallback if upstream cannot expand: a hierarchy model in YAML, queried before building IN. Prefer not to pull that domain into the calc engine.

---

## 10. Calendar and time

- Truncate `time_column` to the first day of `time_grain` and store as `DATE`, never a formatted string.
- Timestamps bucket as stored; there is no timezone conversion, and `time.timezone` is rejected at bind. Convert the column in a `kind: sql` model if a KPI needs it.
- Default calendar: **gregorian**. Add fiscal start-month only when a KPI needs fiscal YTD.
- Anchor is user-selected, so “incomplete current month” is an explicit user choice, not a framework default. Still echo `anchor` in metadata.

`business_date` is never an as-of clock.

---

## 11. Entry point and packaging

### 11.1 Current entry

`udfs.sotif.main` remains the UDF the metadata layer calls (`output_type: df`).

Implement it as a **thin shim**:

```text
udfs.sotif.main(context) → kpi_engine.compute(context) → DataFrame or JSON
```

Later, one generic UDF (`kpi_engine.main`) can replace per-KPI UDF names via metadata only.

### 11.2 Folder structure

```text
udfs/
  sotif/main.py              # thin shim → kpi_engine.compute(context)
  kpi_engine/
    core/
      adapter.py             # context quirks, month-filter claim, single-view assert
      binder.py              # kpi_id → YAML, common measure fields
      loader.py              # load registries; extras allowlist
      op_protocol.py         # OpPlugin façade (needs_time, parse)
      op_registry.py         # name → plugin
      time_planner.py        # anchor, required_span, range predicate
      model_sql.py           # physical YAML or SQL model → DuckDB
      filters.py / filter_ops.py
      cuts.py                # generic cut planner
      calc_engine.py         # dispatch to plugins on the monthly frame
      fn_apply.py            # COLUMN_FNS / MEASURE_FNS maps + Pandas apply
      orchestrator.py        # request lifecycle, one DuckDB session
    capabilities/            # function, op, and hook bodies
    registries/              # YAML allowlist + generated CAPABILITIES.md
    extensions/              # compatibility shims only
    contracts.py
  config/
    models/                  # physical YAML or sql models
    kpis/                    # one file per kpi_id
tests/                       # local parquet, no ADLS
```

### 11.3 Security

- No `eval` of YAML expressions.
- No `importlib` of caller-supplied paths.
- DuckDB queries parameterized; dataset paths from trusted context, not concatenated from filter values.
- Custom logic: registry names only.

### 11.4 Caching (design now, implement later)

Key: `kpi_id`, KPI version, catalog version, resolved filters, cuts, requested outputs, `required_span`, plus a data version (Delta table version). User-selected anchor makes this deterministic.

---

## 12. Onboarding a new KPI

1. Copy a template (`single_model_agg`, `ratio_two_models`, `with_trend`).
2. Point `model` at aliases that exist in context.
3. Declare `time`, `base_measures`, `cuts`, `measures` (every `measure_key` the UI can request).
4. If two models: `model_relations.on` + `how`.
5. Run `validate(sample_context)` — bind errors without ADLS when `output_schema` / column lists exist.
6. If no existing name fits: add the body under `capabilities/` and a row under `registries/`, then regenerate `CAPABILITIES.md`. Do not edit `core/`.

Effort targets:

| KPI type | Author work |
|---|---|
| Simple | ~20 lines of YAML, one model, sum/avg, optional G+R |
| Two-model | YAML only: two bases + derived + `model_relations` |
| Complex | YAML + one registry hook (capabilities + registries, not core) |

Do not put Delta paths, YoY math, or DuckDB connection code in the KPI file.

---

## 13. First implementation slice (`kpi_id` 3004)

**In scope**

- Adapter: single-view assert, `value`/`values`, claim month filter.
- Physical Sotif model bound by alias.
- One additive base measure.
- Cuts G and R.
- Outputs: one point (`previous_year_value` or `current_value`), one window (`value_3m` or `value_12m`), one trend (`trend_12m`).

**Out of scope for the slice**

- Second model / `model_relations`.
- Hierarchy expansion.
- Non-additive aggs.
- Generic shared UDF rename.
- Fiscal calendar.
- Result cache.

**Tests (local parquet, no ADLS)**

1. Missing month in a partition: point lookup is 12 **calendar** months back, not 12 rows.
2. Missing month in a trend: array length unchanged; gap holds its slot.
3. Additive window: regional rows sum to the global row.
4. Scan width: 3m-only request scans 3 periods; adding previous year scans 13; adding `trend_12m` does not scan less than 12.
5. Generated DuckDB SQL **never** contains `IN` on the anchor month alone.
6. Combination absent at the anchor still appears if it has activity in the span (if union-row-set is adopted).

---

## 14. Open items (recommendations, not blockers)

These do not block the first slice. They should be confirmed before many KPIs are authored.

| ID | Topic | Recommendation |
|---|---|---|
| C7 | `heir` filters | Expand upstream; until then reject |
| C8 | Fiscal / timezone | UTC + gregorian DATE buckets; fiscal later |
| I2 | Dataset name | Bind `alias`; confirm stability |
| I4 | Pagination | After calc; sort cut + dimensions; `total_count` |
| I5 | SQL models | Declare `output_schema`; CI `LIMIT 0` probe |
| S2 | Non-additive + cuts | Group cuts by effective WHERE; `GROUPING SETS` when identical |
| S3 | Catalog growth | Version the catalog; KPIs pin version; track hook ratio |
| — | Month filter name | Confirm metadata field / `filter_code` |
| — | Window inclusivity | Default inclusive of anchor |
| — | Row set | Union of combinations across `required_span` |
| — | Trend cuts | Default global cut only |

---

## 15. What was rejected or deferred

| Idea | Status |
|---|---|
| Pandas `eval` of YAML / `df['col'].sum()` as the measure language | Rejected |
| DuckDB as loader only (`SELECT *` then all agg in Pandas) | Rejected for base agg; Pandas still owns KPI math |
| Anchor = `max(time_column)` in the extract | Rejected; user-selected month |
| Anchor = clock / `business_date` | Rejected |
| Infer `measure_key` from catalog op id | Rejected; authored `measures` |
| Standalone API / Databricks job | Out of scope |
| Rewriting context to remove paths | Out of scope |
| Replacing YAML with Python classes | Deferred; YAML + JSON Schema + `validate` |

---

## 16. Glossary

| Term | Meaning |
|---|---|
| Context | Request JSON from the metadata framework |
| Model | DuckDB extract (physical YAML or SQL) |
| KPI YAML | How to calculate; maps `kpi_id` to measures and cuts |
| Anchor | User-selected month; all scalars and trend windows are relative to it |
| `required_span` | Date range scanned so lookbacks have history |
| Cut | Named aggregation grain + filter-ignore policy (G, R, …) |
| Point | One month on the spine |
| Window | Aggregate over months ending at (or exclusive of) the anchor |
| Trend | Array of monthly values for graphs |
| Measure key | Column name in JSON; must exist in KPI `measures` |
| Capability | Allowlisted name (op, hook, column fn, measure fn) in `registries/` |
| Freeze | A new capability name does not edit `core/` or `extensions/` |

---

*Plan compiled 21 Aug 2026 from architecture reviews against sample context `REQ-page-001` / `kpi_id` 3004. Folder layout and catalog freeze updated 23 Aug 2026.*
