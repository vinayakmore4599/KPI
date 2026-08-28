# KPI Engine — Orchestration Flow and Design

**Audience:** Architecture team, tech leads, platform engineers  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md, kpi-framework-plan.md, kpi-onboarding-guide.md

---

## 1. Executive summary

The KPI Engine is an **in-process Python library** invoked by the existing metadata platform. It is **not** a standalone service.

The host sends a **context JSON** (KPI id, requested measures, filters, dataset paths). The engine loads **KPI YAML** and **model YAML**, retrieves data through **DuckDB** (ADLS Parquet/Delta), calculates measures in **Pandas**, and returns a **JSON payload** of table rows plus optional trend arrays.

**Key design choices:**

- **YAML owns all KPI math** — no per-KPI Python branches in the pipeline.
- **DuckDB retrieves; Pandas calculates** — extract and pre-aggregate in SQL; all derived measures in Pandas.
- **Frozen pipeline, extensible catalog** — stage order is fixed; new behavior is added via ops, hooks, and functions in registries.
- **One anchor per request** — the selected reporting period is frozen for every cut and measure.

**Entry point:** `kpi_engine.compute(context)` → `pipeline/orchestrator.py::_compute()`

---

## 2. System boundary

### 2.1 What the engine owns

- Resolve KPI and model YAML by `kpi_id`
- Claim the time selection and compute lookback span
- Compile and run DuckDB retrieve SQL
- Densify a calendar month spine
- Evaluate measure formulas (ops, hooks, functions)
- Paginate, sort, and return JSON

### 2.2 What stays in the host platform

- Building the context JSON from metadata tables
- ADLS credentials and path resolution
- Authentication and authorization
- Hierarchy expansion for heir filters
- UI rendering of rows and charts

```text
  UI / page
      │
      ▼
  Metadata platform ──context JSON──► kpi_engine.compute()
      ▲                                      │
      │                                      ├── DuckDB ◄── ADLS (Parquet/Delta)
      │                                      ├── kpi_config YAML
      └── JSON rows + trends ◄───────────────┘
```

---

## 3. Three configuration layers

| Layer | Location | Purpose | Changes when |
|---|---|---|---|
| Host envelope | Context JSON | Which KPI, measures, filters, paths | UI / metadata request |
| Extract | `kpi_config/models/` | Tables, joins, SQL CTE | New data source or join |
| Calculation | `kpi_config/kpis/` | Time, dims, facts, cuts, formulas | New KPI or measure |
| Plugin bodies | `kpi_engine/capabilities/` | Op, hook, function implementations | New reusable algorithm |
| Allowlists | `kpi_engine/registries/` | Callable names | New catalog entry |
| Frozen pipeline | `kpi_engine/pipeline/` | Stage order, SQL compile, dispatch | Architecture decision only |

A **new KPI** is YAML only. A **new reusable name** (op, hook, function) is capabilities + registries. A **new engine behavior** (agg type, filter stage, time format) requires pipeline work.

---

## 4. Request inputs

Every compute request combines three inputs:

| Input | Carries | Example |
|---|---|---|
| Context JSON | kpi_id, measure_keys, filters, dataset paths, pagination | `execution.kpi_id: 3004` |
| KPI YAML | Time, dimensions, base facts, cuts, measure formulas | `kpi_config/kpis/sotif/3004.yaml` |
| Model YAML | How to query ADLS | `kpi_config/models/sotif/sotif.yaml` |

The context says **what to compute**. KPI YAML says **how**. Model YAML says **what to read**.

Typical context shape:

```json
{
  "execution": {
    "kpi_id": 3004,
    "request_id": "REQ-001",
    "view_details": [{
      "measures_required": [
        { "measure_key": "current_value" },
        { "measure_key": "yoy_month" }
      ]
    }]
  },
  "filters": {
    "reporting_month": { "value": ["2026-07-01"] },
    "region": { "values": ["NA"] }
  },
  "datasets": [{ "alias": "sotif", "path": "abfss://..." }]
}
```

---

## 5. End-to-end orchestration sequence

Every request follows this order. There is no `if kpi_id == …` branch in Python.

```text
  Host context JSON
        │
        ▼
  1. adapt(context)                    → AdaptedRequest
        │
        ▼
  2. load_kpi + load_model             → KpiSpec + ModelSpec
     bind_datasets                     → ADLS paths from context
     resolve_requested_graph            → dependency closure
     partition_request                  → per-model pipelines
        │
        ▼
  3. plan_time                         → anchor + required_span
     bind_filters + split_filters       → extract / calc / result
        │
        ▼
  4. compile_extract + DuckDB execute  → row-level DataFrame
        │
        ▼
  5. apply_pandas_facts                → row helpers, named facts
     collapse_pandas_detail            → GROUP BY extract grain
     densify                            → combo × every month in span
        │
        ▼
  6. compute_cuts                      → ops per combo per cut
        │
        ▼
  7. result filters, green, sort,
     paginate, period wrap              → JSON response
```

### Module map

| Step | Function | File |
|---|---|---|
| Parse context | `adapt` | `pipeline/adapter.py` |
| Load YAML | `load_kpi`, `load_model` | `pipeline/binder.py` |
| Measure graph | `resolve_requested_graph` | `pipeline/binder.py` |
| Split extracts | `partition_request` | `pipeline/pipelines.py` |
| Time selection | `plan_time` | `pipeline/time_planner.py` |
| Filter bind/split | `bind_filters`, `split_filters` | `pipeline/filters.py` |
| SQL compile + run | `compile_extract`, `extract` | `pipeline/model_sql.py` |
| Facts + fold | `apply_pandas_facts`, `collapse_pandas_detail` | `pipeline/fn_apply.py` |
| Dense calendar | `densify` | `pipeline/calc_engine.py` |
| Cuts + ops | `compute_cuts`, `evaluate` | `pipeline/calc_engine.py` |
| Plugin bodies | `OpPlugin.evaluate` | `capabilities/ops/` |
| JSON envelope | `_compute` tail | `pipeline/orchestrator.py` |

`validate(context)` runs the same path through **compile SQL only** — no ADLS scan.

---

## 6. How YAML formulas flow to results

KPI YAML defines calculation at two levels.

### 6.1 base_measures — internal facts

Not returned to the UI. Pulled from DuckDB and aggregated to monthly grain.

```yaml
base_measures:
  sotif_value:
    sql: amount
    agg: sum
```

### 6.2 measures — UI-facing formulas

Requested via `measure_key` in the context. Each has an `op:` (measure kind).

```yaml
measures:
  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }

  value_12m:
    of: sotif_value
    op: window
    trailing: { months: 12 }

  yoy_month:
    op: arithmetic
    fn: growth_pct
    left: current_value
    right: previous_year_value
```

### 6.3 Dispatch path

```text
  measures.current_value: { op: point, of: sotif_value }
        │
        ▼
  registries/ops.yaml  →  Point class (OpPlugin)
        │
        ▼
  calc_engine.evaluate()  →  slice series for one combo
        │
        ▼
  Point.evaluate(EvalCtx)  →  scalar at anchor month
```

Child measures recurse through the same `evaluate()` (e.g. `yoy_month` evaluates `current_value` and `previous_year_value` first). Hooks use `op: hook` → `hook_registry.run()`.

---

## 7. DuckDB vs Pandas — the two-engine split

| Layer | Responsibility | Examples |
|---|---|---|
| DuckDB | Scan ADLS, IN filters, time range, base GROUP BY | `WHERE region IN ('NA')`, `event_month BETWEEN span_start AND anchor` |
| Pandas | All KPI math | `op: window`, `op: point`, `op: rank`, `op: hook` |

DuckDB does **not** run measure formulas. It retrieves physical columns and pre-aggregates to the finest grain any cut needs.

After handoff, Pandas:

1. Applies row-level column functions and facts
2. Folds to extract grain with declared `agg`
3. **Densifies** — every dimension combo × every calendar month in the span
4. Re-aggregates per cut and runs op plugins

**Why densify:** YoY, trailing-N, and lag must move by **calendar months**, not by row order in the extract.

---

## 8. Time planning

The selected reporting period is **claimed** by `plan_time` and removed from generic IN filters. Lookback widens the DuckDB scan; calculation collapses back to the anchor.

Rules:

- Scalar time filter on the context = exactly one value → **anchor**
- Lookback computed from **requested measures only** — unrequested measures do not widen the scan
- DuckDB gets a **date range** (`BETWEEN span_start AND anchor`), never a single-month IN
- Snapshot KPIs (no `time:` block): point at offset 0 and constant only

If the selection is unbounded, a **period probe** SQL finds min/max dates in the data before densify.

---

## 9. Filters — three stages

| Stage | When applied | Effect |
|---|---|---|
| `extract` | Default for host IN lists | Predicate in DuckDB WHERE |
| `calc` | Cut `ignore_filters`, or YAML `apply: calc` | Mask Pandas frame per cut (e.g. G ignores region) |
| `result` | YAML `apply: result` | Drop JSON rows after calculation |

Example: cut G ignores the region filter so global totals include all regions while cut R still filters by region.

---

## 10. Cuts — grouping grains, not measures

Cuts define **how rows are grouped** on the same extract. They are not separate measures.

```yaml
cuts:
  - name: G
    group_by: []
    exclude_from_grain: [region]
    ignore_filters: [region]
    also_emit: [R]
  - name: R
    group_by: [region]
```

Effective grain = `request_grain` − `exclude_from_grain` + `group_by` extras.

One DuckDB extract and one monthly spine feed **both** cuts. Pandas re-aggregates per cut and tags each row with `output_cut`.

---

## 11. Measure evaluation phases

Within `compute_cuts()`, measures run in three phases:

```text
  Monthly densified frame
        │
        ▼
  For each emitted cut
        │
        ├── apply_cut_filters (ignore_filters)
        ├── unique dimension combos
        │
        ▼
  COMBO PHASE (per combo, on that group's time series)
        point, window, trend, arithmetic, fn, hook, lag, …
        │
        ▼
  CUT PHASE (across all combos in the cut)
        rank, percent_of_total, ntile, …
        │
        ▼
  CUT-DERIVED (fn/arithmetic on cut-phase outputs)
        │
        ▼
  JSON row: dims + measure_keys + output_cut
```

| Phase | Input scope | Example ops |
|---|---|---|
| Combo | One combo's aligned monthly series | `point`, `window`, `trend`, `fn`, `hook` |
| Cut | Full set of combos on the cut | `rank`, `percent_of_total` |
| Cut-derived | Cut-phase outputs | `fn` over rank values |

---

## 12. Capability catalog and extensibility

YAML `op: window` is not string evaluation. It resolves through a closed allowlist:

```text
  measures.x: { op: window }
        │
        ▼
  registries/ops.yaml  (module + attr)
        │
        ▼
  loader.ensure_loaded()  at import
        │
        ▼
  op_registry.OP_KINDS  →  capabilities.ops.combo.Window
        │
        ▼
  evaluate(EvalCtx)  at runtime
```

| Registry | Body folder | Used by |
|---|---|---|
| `ops.yaml` | `capabilities/ops/` | `measures.op` |
| `hooks.yaml` | `capabilities/hooks/` | `op: hook` |
| `functions/column.yaml` | `capabilities/functions/column/` | `base_measures` row transforms |
| `functions/measure.yaml` | `capabilities/functions/measure/` | `op: fn` / `arithmetic` |

Unknown or disabled names fail at **bind time**, not at runtime mid-request.

Current catalog scale: ~58 ops, 36 hooks, 46 column functions, 41 measure functions.

---

## 13. Worked example — SOTIF KPI 3004

### Request

- KPI: 3004
- Measures: `current_value`, `yoy_month`
- Anchor: July 2026
- Filter: region = NA

### Engine steps

1. **Bind** — Load `3004.yaml`. Graph: `yoy_month` also needs `previous_year_value`.
2. **Time plan** — Anchor = 2026-07-01. Span back to 2025-07-01 for YoY.
3. **Extract** — DuckDB sums `amount` by reason_code, region, event_month over the span.
4. **Densify** — Every combo gets a row for every month Jul 2025 – Jul 2026.
5. **Cut G** — Ignore region filter; group without region:
   - `current_value` → point at Jul 2026
   - `previous_year_value` → point offset 1 year
   - `yoy_month` → growth_pct of the two
6. **Cut R** — Same measures, grouped by region (via `also_emit`).
7. **Response** — JSON rows with period-wrapped values.

### Sample response row

```json
{
  "output_cut": "G",
  "reason_code": "SUPPLIER",
  "current_value": {
    "value": 8500,
    "period": "2026-07-01",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31"
  },
  "yoy_month": {
    "value": 18.06,
    "period": "2026-07-01"
  }
}
```

---

## 14. Response contract

Key fields returned by `compute()`:

| Field | Meaning |
|---|---|
| `kpi_id`, `request_id` | Echo from request |
| `rows` | One object per dimension combo per cut; one column per requested measure_key |
| `parameters` | Anchor, span_start, time_grain, time_selection |
| `trend_axes` / `trend_labels` | Shared period axis for graph measures |
| `applied_filters` / `ignored_filters` | Filter audit trail by stage |
| `applied_cuts` / `dropped_cuts` | Which cuts ran and why any dropped |
| `notes` / `quality_flags` | Warnings (empty selection, required measure null, etc.) |
| `sql` / `sqls` | Compiled DuckDB SQL for debugging |
| `pagination` | page, page_size, total_count, has_more |

Post-calculation order: result filters → green flag → sort → paginate → dimension roles → period wrap on page rows only.

---

## 15. Design principles

| Principle | Rationale |
|---|---|
| YAML owns math | Onboard KPIs without pipeline edits |
| Frozen pipeline | Predictable stage order; test once, extend via catalog |
| Anchor frozen per request | Consistent period across cuts and measures |
| Lookback from requested measures | Avoid scanning years of data for unused measures |
| Calendar spine | Correct YoY, trailing, lag semantics |
| Cuts are generic | G/R are examples; any grain via YAML |
| Closed catalog | Bind-time validation; no arbitrary Python in YAML |
| DuckDB retrieve, Pandas calc | SQL for I/O scale; Pandas for flexible time logic |

---

## 16. Code and documentation map

| Concern | Primary file / doc |
|---|---|
| Full lifecycle | `kpi_engine/pipeline/orchestrator.py` |
| Context adaptation | `kpi_engine/pipeline/adapter.py` |
| YAML binding | `kpi_engine/pipeline/binder.py` |
| Time anchor + span | `kpi_engine/pipeline/time_planner.py` |
| SQL compile + extract | `kpi_engine/pipeline/model_sql.py` |
| Spine + cut loop | `kpi_engine/pipeline/calc_engine.py` |
| Op implementations | `kpi_engine/capabilities/ops/` |
| Runtime architecture | `kpi-system-architecture.md` |
| Locked product decisions | `kpi-framework-plan.md` |
| Capability catalog | `kpi_engine/registries/CAPABILITIES.md` |
| Excel capability workbook | `docs/KPI-Engine-Capabilities.xlsx` |

---

## 17. Summary

```text
  Context picks KPI + measures
        +
  KPI YAML defines facts + formulas
        +
  Model YAML defines DuckDB retrieve
        │
        ▼
  DuckDB: scan, filter, pre-aggregate
        │
        ▼
  Pandas: densify calendar spine → cuts → ops/hooks
        │
        ▼
  JSON rows with values, trends, and audit metadata
```

The orchestrator is the conductor: it never implements KPI math itself. It binds YAML, plans time, extracts once (per pipeline), densifies, dispatches the catalog, and shapes the response. All business logic lives in YAML formulas and registered plugins.

---

## 18. Presentation talking points

1. **Separation of concerns** — Host builds context; engine computes; YAML configures; catalog extends.
2. **Scale path** — New KPI = YAML file. New algorithm = registry entry. Pipeline stays frozen.
3. **Time correctness** — Dense calendar spine + frozen anchor avoids subtle off-by-one and YoY bugs.
4. **Multi-cut efficiency** — One extract powers G and R (and more) via re-aggregation, not re-query.
5. **Auditability** — Response includes SQL, applied/ignored filters, notes, and quality flags.
6. **Validate without scan** — `validate(context)` compiles SQL for review before production runs.
