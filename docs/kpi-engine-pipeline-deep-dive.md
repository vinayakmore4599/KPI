# KPI Engine — Pipeline Deep Dive

**Audience:** Architecture team, senior engineers, platform owners  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md, KPI-Engine-Orchestration-Guide.docx, kpi-framework-plan.md

---

## 1. Purpose of this document

This document explains **every stage** of `kpi_engine.compute(context)` in implementation detail: what runs, in what order, which module owns it, what data structures pass between stages, and how YAML formulas become JSON results.

The orchestrator (`pipeline/orchestrator.py::_compute`) is the conductor. It never implements KPI math directly — it binds configuration, plans time, extracts data, dispatches the capability catalog, and shapes the response.

---

## 2. Pipeline overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  PHASE A — BIND (no data scan)                                          │
│  adapt → load_kpi → bind_request → resolve_graph → partition_request    │
│         → bind_datasets → plan_time → bind_filters → prepare_pipeline   │
├─────────────────────────────────────────────────────────────────────────┤
│  PHASE B — EXTRACT (DuckDB)                                             │
│  compile_extract → execute SQL → raw row-level / pre-aggregated frame   │
├─────────────────────────────────────────────────────────────────────────┤
│  PHASE C — TRANSFORM (Pandas)                                           │
│  fold → dimension maps → calc filters → facts → collapse → densify      │
├─────────────────────────────────────────────────────────────────────────┤
│  PHASE D — CALCULATE (Pandas + OpPlugins)                               │
│  compute_cuts → combo phase → cut phase → cut-derived → having          │
├─────────────────────────────────────────────────────────────────────────┤
│  PHASE E — ENVELOPE (orchestrator)                                      │
│  result filters → green → sort → paginate → period wrap → JSON          │
└─────────────────────────────────────────────────────────────────────────┘
```

Entry: `kpi_engine.compute(context)`  
Validate-only: same through compile SQL, no file scan (`validate(context)`).

---

## 3. Step 1 — Adapt (parse context JSON)

**Module:** `pipeline/adapter.py` → `adapt(context)`  
**Input:** Raw host context dict  
**Output:** `AdaptedRequest` (typed, normalized)  
**Errors:** `ContextError`, `FilterError`, `BindError`

### 3.1 What happens

1. Validates `context` is a JSON object.
2. Reads `execution.kpi_id` (required).
3. Requires **exactly one** `execution.view_details` entry (standard mode).
4. Reads measure list from `measures_required` or `measures_requested` on the view or execution block.
5. Normalizes every filter: `value` or `values` → list; default operator IN.
6. Parses `datasets` into `DatasetBinding` objects (alias, path, table_type, columns, mappings).
7. Parses `output` into `Pagination` (page, page_size, limit, trend_page, trend_page_size).
8. Parses top-level `parameters` as JSON scalars, lists, or flat dicts.
9. Reads `selected_dimensions` if present.
10. Rejects `execution.time_grain` (must use `parameters.time_grain`).
11. Rejects filters with `input_text: heir` (hierarchy expansion is upstream).
12. Ignores `business_date` entirely.

### 3.2 Key fields on AdaptedRequest

| Field | Meaning |
|---|---|
| `kpi_id` | Selects KPI YAML file by stem |
| `measure_keys` | Requested measure_key tuple; empty if omitted |
| `measures_omitted` | True when host did not send a measure list |
| `filters` | Tuple of IncomingFilter(code, values, input_text) |
| `datasets` | Tuple of DatasetBinding |
| `parameters` | Dict from context.parameters |
| `selected_dimensions` | Optional grain override |
| `pagination` | Page sizing |
| `raw` | Original context (for logging) |

### 3.3 What adapt does NOT do

- Does not load KPI YAML
- Does not claim the reporting-month filter (time planner does)
- Does not validate measure keys against KPI catalog
- Does not bind dataset paths to model aliases

### 3.4 Example

**Input filter:**

```json
"reporting_month": { "value": ["2026-07-01"] }
```

**Becomes:** `IncomingFilter(code="reporting_month", values=("2026-07-01",), ...)`

This filter is still in the list at this point. Step 7 removes it from generic IN binding.

---

## 4. Step 2 — Load KPI YAML

**Module:** `pipeline/binder.py` → `load_kpi(kpi_id, config_dir, parameters, selected_dimensions)`  
**Input:** kpi_id + bound parameters + grain selection  
**Output:** `KpiSpec` (partially bound; measures not yet desugared/validated)  
**Errors:** `BindError`, file-not-found

### 4.1 Resolve file path

- Searches `kpi_config/kpis/<group>/<kpi_id>.yaml`
- Filename stem must **exactly** match `kpi_id` (no fold)
- Reads YAML into a raw dict

### 4.2 Parse time block

**Function:** `_parse_time()`

```yaml
time:
  column: event_month
  grain: month
  filter_code: reporting_month
  calendar: gregorian
  periods: [year, month]        # optional multi-part selection
  anchor_mode: last_observed    # optional
  max_span_years: 10            # optional safety cap
```

Produces `TimeSpec`. If `time:` is omitted, KPI is a **snapshot** (no period clock).

### 4.3 Parse parameters schema

**Function:** `_parse_parameters()` + `parameters.bind_incoming()`

```yaml
parameters:
  threshold:
    type: float
    default: 95
  time_grain:
    type: string
    default: month
```

- Coerces `context.parameters` against declared types
- Rejects unknown parameter names
- Reserved names: `time_grain`, `output_cut`, `only_cut`, `emit_cuts`, `selected_dimensions`
- User parameter names must not collide with YAML `measures:` keys

### 4.4 Resolve templated KPI

**Function:** `resolve.resolve_kpi()`

If YAML uses `when:` / `from_param:` overlays, selects the matching body based on bound parameters before parsing. Supports multi-case KPI templates without separate files.

### 4.5 Parse full KpiSpec

**Function:** `_parse_kpi()`

Builds typed structures:

| YAML block | Parsed to |
|---|---|
| `dimensions` | `DimensionSpec` tuple (name, source, cardinality, maps) |
| `default_dimensions` | Default request grain |
| `base_measures` | `BaseMeasure` tuple (sql, agg, columns, expr, where, model_id) |
| `cuts` | `CutSpec` tuple (name, group_by, exclude_from_grain, ignore_filters, also_emit) |
| `measures` | `OutputSpec` tuple via each op's `parse()` |
| `filters` | KPI-level filter specs (column, op, apply stage) |
| `model_relations` | Join keys for multi-model KPIs |
| `green_when`, `having`, `sort`, `max_rows` | Response shaping rules |

Each measure calls `get_op(kind).parse(key, common_fields)` to build an `OutputSpec`.

### 4.6 Apply bound parameters to spec

**Function:** `parameters.apply_bound_to_spec()`

Overlays:
- `time_grain` → effective grain on TimeSpec
- `output_cut` / `only_cut` / `emit_cuts` → cut walk control
- User params → available in measure expr/fn kwargs

### 4.7 Apply request grain

**Function:** `apply_request_grain()`

| selected_dimensions | Resulting request_grain |
|---|---|
| Omitted | YAML `default_dimensions` |
| Empty list | Empty grain (global) |
| Name list | Validated against dimension catalog |
| Bool map | `{ dim: true }` entries become grain |

Time column cannot appear in selected_dimensions.

### 4.8 Load and validate models

- Loads each model referenced by base_measures
- Asserts default grain columns exist on model schema
- Validates `when:` cases if overlays present

---

## 5. Step 3 — Bind request (measure validation pass)

**Module:** `binder.bind_request()` — called from orchestrator `_apply_host_defaults()`  
**Purpose:** Final measure-level bind after host defaults applied

### 5.1 Host measure defaults

If `measures_omitted` and YAML has `meta.selected_metrics`, those become the request list.

### 5.2 Fold measure keys

**Function:** `fold_measure_keys()`

Expands dual-key measures (e.g. `forecast_confidence` → `_low` + `_high` if configured).

### 5.3 Assert measure keys exist

Every requested `measure_key` must exist under YAML `measures:` — otherwise BindError.

### 5.4 Apply request time grain

**Function:** `time_planner.apply_request_time()`

Overlays `parameters.time_grain` onto `kpi.time.grain` for this request only (e.g. month → quarter).

### 5.5 Desugar compare ops

YAML sugar `op: compare` (MoM/WoW/QoQ shorthand) expands to real ops (`pct_change`, `point`, etc.) so lookback and validation see true dependencies.

### 5.6 Validate every measure

```python
for spec in kpi.measures:
    get_op(spec.kind).validate(spec, kpi)
```

Each OpPlugin checks:
- Required `of:` / `inputs:` / `fn:` present
- References point to known bases or measures
- Offset/trailing legal for snapshot KPIs
- Window range valid
- No unknown YAML keys for this op kind

### 5.7 Assert dependency graph

- No cycles in measure dependencies
- All referenced keys exist
- `from_cut` / `versus_cut` grains compatible

### 5.8 Snapshot guard

KPIs without `time:` may only use snapshot-safe ops (point offset 0, constant, dimension echo).

---

## 6. Step 4 — Resolve requested measure graph

**Module:** `binder.resolve_requested_graph(kpi, measure_keys)`  
**Output:** `(expanded_keys, needed_base_measures)`

### 6.1 Walk algorithm

Starting from requested keys, recursively collects:

1. **Measure dependencies** — via `get_op(kind).dependencies(spec)`
   - `arithmetic` → left/right or `of` operands
   - `fn` → `inputs` list
   - `expr` → referenced measure names
   - `rank` → `of` measure
2. **Base measures** — via each measure's `of:` field
3. **Auxiliary keys** — `green_when.of`, `having` predicates, per-measure `having`

### 6.2 Example expansion

**Request:** `[yoy_month]`

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value
  right: previous_year_value

current_value:
  op: point
  of: sotif_value

previous_year_value:
  op: point
  of: sotif_value
  offset: { years: 1 }
```

**Expanded keys:** `yoy_month`, `current_value`, `previous_year_value`  
**Needed bases:** `sotif_value`

### 6.3 Important rule

Empty `measures_required` computes **nothing**. It does not default to all YAML measures unless host omitted the list AND YAML defines `meta.selected_metrics`.

Unrequested measures **never widen** DuckDB lookback or extract columns.

---

## 7. Step 5 — Partition request into pipelines

**Module:** `pipelines.partition_request(kpi, requested)`  
**Output:** List of `Pipeline` objects

### 7.1 Purpose

Groups measures by which **model** their base facts come from. Each pipeline is one DuckDB extract (or one joined extract).

### 7.2 Pipeline struct

```text
Pipeline:
  model_ids:    ("sotif",) or ("orders", "shipments")
  measure_keys: measures this extract serves
  bases:        base_measures to retrieve
  joined:       True when multi-model join after extract
```

### 7.3 Rules

- Single-model measures → one pipeline per model
- Measure spanning models → requires `model_relations` in KPI YAML; creates joined pipeline
- Join happens **after** each model is aggregated to monthly grain

---

## 8. Step 6 — Bind models and datasets

**Modules:** `binder.load_model()`, `binder.bind_datasets()`

### 8.1 Load model YAML

KPI `model: sotif` → `kpi_config/models/sotif/sotif.yaml`

Model kinds:
- **physical** — tables + joins; `read_parquet(?)` or `delta_scan(?)`
- **sql** — CTE body wrapped as subquery; `$alias_scan` tokens become bound params

### 8.2 Bind datasets

For each `required_aliases` on the model:

```text
1. Match context.datasets by alias (case-insensitive)
2. Fall back to datasets key
3. Context path wins over model default_path
4. Missing required alias → BindError
```

Produces `dict[alias, DatasetBinding]` with ADLS path, table_type, column list, filter_column_mappings.

### 8.3 Filter column union

**Function:** `_union_extract_columns()`

Collects every column any filter might bind to across all loaded models — used in filter binding.

---

## 9. Step 7 — Plan time

**Module:** `time_planner.plan_time(request, kpi)`  
**Output:** `(TimePlan | None, remaining_filters)`  
**Errors:** `TimePlanError`

This is the most complex bind-stage step. It determines **anchor**, **span**, and removes time from generic filter binding.

### 9.1 Snapshot KPIs (no time block)

Returns `(None, all_filters)` — no time claim, no span, no densify clock.

### 9.2 Path A — Legacy scalar filter_code

When KPI declares `time.filter_code: reporting_month` and context sends exactly one value:

```text
1. claim_month_filter() removes reporting_month from remaining filters
2. parse_date(value) → raw date
3. truncate_period() → anchor bucket (e.g. 2026-07-01 for month grain)
4. TimeSelection: start=end=anchor, anchor_source="legacy"
5. span_for_keys() widens span_start by lookback
```

**Critical rule:** This filter never becomes `WHERE event_month IN ('2026-07-01')`. It becomes a **range** in DuckDB.

### 9.3 Path B — Multi-part periods

When KPI declares `time.periods: [year, month, ...]`:

```text
1. read_period_parts() reads year/month/… filters separately
2. Parts conjoin (all must match)
3. Missing part = not applied (partial selection)
4. Year-bounded → anchor = last period in selection
5. No year bound → unbounded plan (probe later)
```

### 9.4 Path C — Compose template

When KPI declares `time.compose:`:

```text
1. expand_compose() builds one date from multiple filter codes
2. Treat as scalar anchor (like Path A)
3. Consumed compose keys stripped from remaining filters
```

### 9.5 Path D — Unbounded (whole history)

No scalar, no parts, no compose:

```text
anchor = None
span_start = None
lookback_months = max across requested measures
anchor_source = "data"
→ probe SQL runs later in _resolve_time_plan()
```

### 9.6 Lookback computation

**Function:** `max_lookback_months()` → per-measure `lookback_for()` → `OpPlugin.lookback()`

Each op declares how many grain periods it needs before anchor:

| Op / config | Lookback logic |
|---|---|
| `point` offset 0 | 0 |
| `point` offset 1 year | 12 months (month grain) |
| `window` trailing 12 | 12 months |
| `trend` trailing 12 | 12 months |
| `fn` | max lookback of all inputs (recursive) |
| `lag` n | n periods |

Lookback uses **requested measures only** (after graph expansion).

### 9.7 span_for_keys — final span

```text
span_start = min(
    anchor - lookback,
    earliest period in selection,
    earliest shifted child anchor
)
span_end   = anchor + 1 + lookforward
```

Each pipeline may call `span_for_keys` again with its subset of measures.

### 9.8 Period probe (unbounded / last_observed)

**Function:** `orchestrator._resolve_time_plan()`

When anchor is unknown:
1. Runs `compile_period_probe` SQL (min/max dates with extract filters)
2. `fill_probed_selection()` sets anchor and span from observed data
3. Notes emitted if selection empty

### 9.9 apply_year_basis

If host sent a year filter part, forces calendar Jan–Dec year boundaries.

### 9.10 TimePlan output struct

```text
TimePlan:
  anchor:              date | None     — frozen reporting period
  span_start:          date | None     — earliest month to retrieve
  span_end_exclusive:  date | None     — exclusive upper bound
  lookback_months:     int
  lookback_forward:    int             — for leading windows
  claimed_filter_code: str
  selection:           TimeSelection   — parts, start, end, periods, anchor_source
```

---

## 10. Step 8 — Bind filters

**Module:** `filters.bind_filters(remaining, kpi, datasets, extract_columns)`  
**Output:** `(bound_filters, skipped_filters)`

### 10.1 Mapping sources (priority)

1. KPI YAML `filters:` block (explicit column, op, apply stage)
2. Dataset `filter_column_mappings` (code → column)
3. KPI `filter_map` (optional alias map)

### 10.2 Per-filter processing

For each remaining IncomingFilter:

```text
1. Resolve FilterApplySpec from KPI filters: or default
2. Default op: IN
3. Blank / empty / all-null values → skip (record in skipped_filters)
4. Snapshot KPI + time-like code → skip
5. Map code → column; unmapped valued filter → BindError
6. Emit BoundFilter(code, column, op, values, stage)
```

### 10.3 BoundFilter struct

```text
BoundFilter:
  code:    filter code from context
  column:  physical or logical column name
  op:      in | eq | gt | lt | is_null | ...
  values:  tuple of bound values
  stage:   extract | calc | result
```

---

## 11. Step 9 — Split filters by stage

**Module:** `filters.split_filters(bound, emitted_cuts, extra_ignored)`

### 11.1 Three buckets

| Bucket | Applied where | Typical content |
|---|---|---|
| `source_filters` (extract) | DuckDB WHERE | region, supplier, status |
| `deferred` (calc) | Pandas per cut | Filters ignored by cut G |
| `result_filters` (result) | JSON row drop | Post-calc visibility rules |

### 11.2 ignore_filters interaction

For each emitted cut, filters listed in `cut.ignore_filters` are **promoted from extract to calc**:

```yaml
cuts:
  - name: G
    ignore_filters: [region]
```

- Cut G calc frame: region filter **not applied** → global totals
- Cut R calc frame: region filter **applied** → regional breakdown
- Same DuckDB extract serves both

### 11.3 Measure-level ignore_filters

Measures with `ignore_filters:` on a base `of:` clone a filtered fact; those codes are also tracked for calc-stage routing.

---

## 12. Step 10 — Prepare pipeline

**Module:** `orchestrator._prepare_pipeline()`  
**Output:** dict with models, datasets, cuts, grain, plan, filter tuples

### 12.1 Compatible cuts

```text
1. cuts_for_keys(kpi, pipe.measure_keys) → candidate cuts
2. compatible_cuts(candidates, available_columns, time_col) → emitted
3. Incompatible cuts → dropped_cuts with reason "incompatible_extract"
4. assert default_cut / locked_cut still present
```

### 12.2 Extract grain

**Function:** `cuts.extract_grain()`

```text
grain = time_column
      + union(effective_group_by for each emitted cut)
      + join keys (multi-model)
      + filter columns that must survive GROUP BY
      + partition keys for non-additive aggs
```

Finer grain = more rows from DuckDB, but enables all cuts from one extract.

### 12.3 Per-pipeline time span

`plan = span_for_keys(kpi, pipe.measure_keys, time_plan)` — pipeline may need less lookback than full request.

### 12.4 Global vs measure calc filters

- `global_calc` — calc filters applying to all cuts
- `measure_calc` — filters tied to measure-level ignore_filters on bases

---

## 13. Step 11 — DuckDB extract

**Module:** `model_sql.compile_extract()` + `extract()`  
**Called from:** `orchestrator._extract_all()`

### 13.1 SQL compilation

```text
1. Resolve FROM clause (physical scan or SQL subquery)
2. Build SELECT list: grain columns + physical fact columns
3. WHERE: extract-stage BoundFilters as parameterized predicates
4. WHERE: time range  event_month >= span_start AND event_month < span_end
5. GROUP BY: extract grain (for additive aggs at retrieve)
6. Bind ? parameters (paths, filter values, dates)
```

### 13.2 What DuckDB returns

- **Row-level detail** when non-additive aggs needed (count_distinct, median, …)
- **Pre-aggregated monthly rows** for additive sum/count/min/max
- Physical column names only — no measure formulas

### 13.3 Example compiled intent

```sql
SELECT
  reason_code,
  region,
  event_month,
  SUM(amount) AS sotif_value
FROM delta_scan(?)
WHERE event_month >= ?
  AND event_month < ?
  AND region IN (?)
GROUP BY reason_code, region, event_month
```

### 13.4 Multi-model join

When `pipeline.joined`:
1. Each model extracted separately to monthly grain
2. `relations.join_monthly()` joins on declared keys + time + grouping dims

---

## 14. Step 12 — Pandas transform chain

**Module:** `orchestrator._extract_all()` sub-steps

### 14.1 fold_extract_columns

Aligns DuckDB column names to KPI dimension / base_measure names.

### 14.2 apply_dimension_maps

Applies YAML dimension value maps (code → label).

### 14.3 apply_frame_filters (global calc)

Applies calc-stage filters that apply to all cuts before cut loop.

### 14.4 stabilize_detail

Normalizes detail frame types and column order for fact pipeline.

### 14.5 apply_pandas_facts

**Module:** `fn_apply.apply_pandas_facts()`

On row-level detail, in order:
1. Column functions (`base_measures.columns` + `op:`)
2. Row helpers: `expr`, `lookup`, `over`
3. Named facts with `where:` / `also_where:` filters
4. Measure-calc filter scoping via context var

Produces enriched detail with computed fact columns.

### 14.6 collapse_pandas_detail

**Function:** `fn_apply.collapse_pandas_detail()`

GROUP BY extract grain; apply declared `agg:`:

| agg | Behavior |
|---|---|
| sum, count, min, max | Standard aggregation |
| avg | Carried as sum + count; divided after re-agg |
| count_distinct, median, percentile | Stay at detail; re-read per cut |

Output: monthly (or snapshot) aggregated frame per model.

### 14.7 join_monthly (if multi-model)

Joins model frames on relation keys.

### 14.8 densify — calendar spine

**Module:** `calc_engine.densify()`

```text
For each dimension combo seen in extract:
  Cross join with every calendar period from span_start to anchor (+ forward)
  Left join actual values
  Mark _observed flag
  Fill 0 for sum/count up to last observed month; null for other aggs
```

**Why:** `offset: { years: 1 }` on July 2026 must read July 2025 by calendar, not "previous row."

**Cap:** 50,000 combo × period cells (`TREND_CELL_CAP`).

### 14.9 row_set modes

| Mode | Combo set |
|---|---|
| `span_union` (default) | Combos seen anywhere in span |
| `anchor_only` | Combos with observed data at anchor only |

---

## 15. Step 13 — Compute cuts (measure evaluation)

**Module:** `calc_engine.compute_cuts()`  
**Input:** Densified monthly frame, KpiSpec, emitted cuts, TimePlan, requested keys  
**Output:** JSON row list, trend_axes, dropped_groups

### 15.1 Outer loop — cuts

For each emitted cut (respecting `also_emit`, `output_cut`, `emit_cuts` walk):

```text
1. monthly_for(cut) — slice/re-aggregate frame for cut grain
2. apply_cut_filters on detail (ignore_filters)
3. Build combo_frame = unique dimension combos
4. anchor_only filter on combos if configured
5. _evaluate_combos → per-combo measure values
6. apply_to_cut for cut-phase ops
7. _apply_cut_derived for fn/expr on cut outputs
8. apply having (drop groups)
9. Append rows with output_cut tag
```

### 15.2 Inner loop — combos (_evaluate_combos)

For each dimension combo in the cut:

```text
1. _combo_series() — slice monthly DataFrame to this group's time series
2. Build row dict: output_cut, grouped_dimensions, dim values
3. For each measure in eval_need:
     evaluate(spec, series, ...) → scalar or trend array
4. Memo keyed by (measure_key, effective_anchor) prevents duplicate work
```

### 15.3 evaluate() dispatch

**Module:** `calc_engine.evaluate()`

```python
plugin = get_op(spec.kind)
return plugin.evaluate(EvalCtx(
    spec, series, kpi, plan, catalog, detail,
    combo, group_dims, memo, cut, evaluate_fn, anchor, selection
))
```

Child measures call `evaluate_fn(child_spec, anchor=shifted_date)` recursively.

### 15.4 Phase 1 — Combo-phase ops

Run per combo on that group's aligned monthly series:

| Op | What evaluate() does |
|---|---|
| `point` | Read fact column at anchor (or offset anchor) |
| `window` | Sum/avg/min/max over trailing/PTD/YTD range |
| `trend` | Return list of values + register trend_axes |
| `arithmetic` | Evaluate left/right inputs, apply measure fn |
| `fn` | Evaluate all inputs, call registered measure function |
| `expr` | Evaluate input measures, eval expression |
| `hook` | Call hook_registry.run(name, series, …) |
| `lag`, `pct_change` | Shift series by calendar periods |
| `constant` | Return fixed value |
| `dimension` | Echo dimension column value |

### 15.5 Phase 2 — Cut-phase ops

Run after all combos exist (need full cut partition):

| Op | What apply_to_cut() does |
|---|---|
| `rank` | Rank measure values across all combos |
| `percent_of_total` | Each value / sum of cut |
| `ntile`, `dense_rank` | Bucket or rank across cut |
| `gap_to_leader` | Distance from cut maximum |

Cut-phase ops call `source_for_cut()` during combo phase to stash inputs.

### 15.6 Phase 3 — Cut-derived

Measures whose inputs are cut-phase outputs — evaluated after `apply_to_cut()`.

### 15.7 having

YAML `having:` drops groups matching predicates before rows are emitted. `then_group_by` can roll up further.

### 15.8 Silent cuts (versus_cut / from_cut)

Cuts referenced by `versus_cut` or `from_cut` may be computed silently (not emitted) to supply comparison or overlay values.

---

## 16. Step 14 — Post-process and JSON envelope

**Module:** `orchestrator._compute()` tail

### 16.1 apply_result_filters

Drop JSON rows matching result-stage filters (after all calculation).

### 16.2 _stamp_green

If YAML `green_when:` configured, set `row.green` boolean from threshold on named measure.

### 16.3 _sort_rows

Deterministic sort:
1. output_cut
2. request_grain dimensions
3. remaining catalog dimensions
4. YAML `sort:` overrides (asc/desc per key)
5. `max_rows` trim

### 16.4 _paginate

Slice rows by page/page_size. Null page_size = return all rows. Includes total_count and has_more.

### 16.5 _stamp_dimension_roles

For rolled-up cuts (G), sets non-grain dimensions to null in response while keeping column keys aligned across cuts.

### 16.6 _wrap_timed_measures

Wraps numeric measure values:

```json
{
  "value": 8500,
  "period": "2026-07-01",
  "period_start": "2026-07-01",
  "period_end": "2026-07-31"
}
```

Trend measures become list of `{ period, value }` objects. Uses `OpPlugin.periods()` for metadata.

Composite ops (fn, arithmetic, expr) inherit period from inputs when all share one shifted period.

### 16.7 _paginate_trends

Separate pagination for trend array length (trend_page / trend_page_size). trend_axes stays full length.

### 16.8 Response assembly

Final dict includes: rows, parameters, applied_filters, ignored_filters, skipped_filters, applied_cuts, dropped_cuts, notes, quality_flags, trend_axes, trend_labels, sql, pagination, meta.

---

## 17. End-to-end timeline — SOTIF KPI 3004

**Request:** current_value + yoy_month, reporting_month=2026-07-01, region=NA

| # | Stage | Result |
|---|---|---|
| 1 | adapt | measure_keys=(current_value, yoy_month), filters parsed |
| 2 | load_kpi | KpiSpec: time on event_month, cuts G/R, base sotif_value |
| 3 | bind_request | Measures validated; compare desugared if any |
| 4 | resolve_graph | Adds previous_year_value; needs sotif_value base |
| 5 | partition | One pipeline, model sotif |
| 6 | bind_datasets | sotif → abfss:// path from context |
| 7 | plan_time | anchor=2026-07-01, span_start=2025-07-01; reporting_month claimed |
| 8 | bind_filters | region → column region, stage extract |
| 9 | split_filters | region extract; on G promoted to calc ignore |
| 10 | prepare | cuts G+R emitted, grain=[reason_code, region, event_month] |
| 11 | extract | SQL Jul 2025–Jul 2026, SUM(amount) by combo × month |
| 12 | densify | Every reason_code × region × month filled |
| 13 | compute G | Ignore region; point Jul 2026; YoY vs Jul 2025 |
| 14 | compute R | region=NA; same measures per region |
| 15 | wrap | Period metadata on each cell |
| 16 | response | rows[], sql, applied_filters, parameters |

---

## 18. Bind-time vs runtime errors

| When | Error type | Examples |
|---|---|---|
| adapt | ContextError | Missing kpi_id, wrong view_details count |
| load_kpi / bind_request | BindError | Unknown measure, bad parameter, invalid YAML |
| plan_time | TimePlanError | Multiple reporting months, span too wide |
| bind_filters | FilterError / BindError | Unmapped filter, invalid op |
| compute_cuts | CalcError | Missing dimension in combo |
| densify | CatalogError | Grid exceeds 50k cells |

Bind errors fail before any DuckDB scan. Time/filter errors prevent partial wrong results.

---

## 19. Operation reference — what each op does at runtime

### Combo-phase (per group, on monthly series)

| Op | Input | Output | Lookback source |
|---|---|---|---|
| point | of base or measure | Scalar at anchor ± offset | offset |
| window | of base | Scalar aggregate over range | trailing / range |
| trend | of base | List + axis | trailing |
| arithmetic | left/right measures | Scalar | max of inputs |
| fn | inputs + measure fn | Scalar | max of inputs |
| expr | inputs + expression | Scalar | max of inputs |
| hook | of + hook name | Scalar (or low/high) | trailing on measure |
| lag / lead | of measure | Scalar at shifted period | abs(offset) |
| pct_change | of measure | Scalar % change | lag lookback |
| constant | value in YAML | Fixed scalar | 0 |
| dimension | dimension name | Dimension value | 0 |

### Cut-phase (across all groups on cut)

| Op | Needs | Output |
|---|---|---|
| rank | of measure values for all combos | Rank integer per row |
| percent_of_total | of measure values | Share 0–1 per row |
| ntile | of measure values | Bucket 1–N |
| gap_to_leader | of measure values | Delta from max |

---

## 20. Code map by pipeline stage

| Stage | Primary module | Key functions |
|---|---|---|
| Adapt | adapter.py | adapt |
| Load KPI | binder.py | load_kpi, _parse_kpi, bind_request |
| Parameters | parameters.py | bind_incoming, apply_bound_to_spec |
| Resolve | resolve.py | resolve_kpi |
| Graph | binder.py | resolve_requested_graph |
| Pipelines | pipelines.py | partition_request |
| Time | time_planner.py | plan_time, span_for_keys, lookback_for |
| Filters | filters.py | bind_filters, split_filters |
| SQL | model_sql.py | compile_extract, extract |
| Facts | fn_apply.py | apply_pandas_facts, collapse_pandas_detail |
| Spine | calc_engine.py | densify, compute_cuts, evaluate |
| Ops | capabilities/ops/ | OpPlugin.evaluate, apply_to_cut |
| Hooks | capabilities/hooks/ | hook_registry.run |
| Orchestrator | orchestrator.py | _compute, _extract_all, _prepare_pipeline |

---

## 21. Design invariants (architecture checklist)

1. **One anchor per request** — frozen across cuts and measures
2. **Time filter never a one-month IN** — always a range in DuckDB
3. **Lookback from requested graph only** — no scan widening for unused measures
4. **DuckDB retrieve, Pandas calculate** — no measure formulas in SQL
5. **Calendar spine** — densify before any offset/window/lag
6. **Closed catalog** — op names resolve through registries at bind
7. **One extract, many cuts** — re-aggregate in Pandas, not re-query
8. **Three filter stages** — extract / calc / result with ignore_filters per cut
9. **Deterministic output** — sort before paginate; stable row order
10. **Audit trail** — SQL, applied/ignored filters, notes in every response

---

## 22. Summary

The KPI engine transforms **context + YAML** into **JSON** through a fixed five-phase pipeline:

```text
BIND  →  plan anchor, validate formulas, map filters
EXTRACT  →  DuckDB retrieves physical columns over time range
TRANSFORM  →  Pandas facts, fold, densify calendar spine
CALCULATE  →  cuts × combos × op plugins → measure values
ENVELOPE  →  sort, paginate, wrap periods, return audit metadata
```

Every measure formula in KPI YAML is ultimately executed by an **OpPlugin.evaluate()** (or **apply_to_cut()** for cut-phase ops) inside **compute_cuts**, operating on a dense monthly series built from a single DuckDB retrieve per pipeline.

Understanding bind (Steps 1–10) explains **what will run and how much data will be scanned**. Understanding extract and densify (Steps 11–12) explains **why YoY and trailing windows are calendar-correct**. Understanding compute_cuts (Step 13) explains **how YAML ops become numbers in each JSON row**.
