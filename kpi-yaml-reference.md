# KPI YAML reference

Everything the engine can calculate, and how to declare it in YAML.

Use this document while writing `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` and `kpi_config/models/<kpi_group>/<model_id>.yaml`.

Related docs:

- [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) — live catalog of every op, function, and hook. New names go in `capabilities/` + `registries/` only; do not edit `pipeline/`.
- [kpi-yaml-ai-prep.md](kpi-yaml-ai-prep.md) — **AI YAML authoring** (compact bind-ready contract + checklist)
- [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md) — human deep-dive: function catalog, columns vs expressions, when to use what, limits
- [kpi-onboarding-guide.md](kpi-onboarding-guide.md) — the step-by-step process and which files to change
- [kpi-system-architecture.md](kpi-system-architecture.md) — runtime flow, stages, and diagrams
- [README.md](README.md) — folders, install, request path
- [kpi-framework-plan.md](kpi-framework-plan.md) — architecture and locked decisions

---

## Ownership

**YAML owns calculation.** `base_measures`, `measures` (`op`, `agg`, `fn`, `expr`, offsets, windows, trends), `green_when`, `row_set`, `data_points`, and filter *ops* do not change with grain. Adding a catalog dimension does not require new measure keys or DuckDB formulas.

**YAML owns the grouping allowlist and cut variants.** `dimensions:` (name + `from:`), `default_dimensions`, `cuts[].group_by` (extras only), `cuts[].exclude_from_grain`, `ignore_filters`, `also_emit`, `measures.*.cuts`.

**The request owns only which allowlisted dims are active this call** (`context.selected_dimensions`, or YAML `default_dimensions` if omitted). That is GROUP BY keys, not math. Do not declare `parameters.selected_dimensions`.

Uniform means the same formula at every grain, not one grain per request. A G+R response still runs `current_value` twice (two YAML cuts). Selecting `region` still leaves G without region if YAML `exclude_from_grain` says so.

---

## Breaking changes (request-time dimensions)

Hosts and YAML authors must treat these as breaks, not silent extras:

- **YAML:** `default_dimensions` is required (`[]` is a legal worldwide default). `cuts[].group_by` lists extras only. New `exclude_from_grain`. `group_by ∩ default_dimensions` and `exclude_from_grain ∩ group_by` are bind errors. Dim-named `ignore_filters` must match `exclude_from_grain` both ways.
- **Rows:** required `grouped_dimensions` (effective grain for that `output_cut`, may be `[]`). Dimensions not in that grain stay on the row as `null`.
- **Result filters:** `apply: result` on a dim not in that cut’s effective grain is skipped (`not_in_grain`). Region IN no longer hides G by matching stamped null.
- **Hosts that drop rows when all catalog dim columns are null** must use `grouped_dimensions` / `applied_cuts`. Worldwide G is valid.
- **Payload:** `selected_dimensions`, `applied_cuts`, `dropped_cuts`, `dropped_groups`, `grain_warnings`.

---

## 1. Cheat sheet

```yaml
kpi_id: 3004                 # must match execution.kpi_id
version: 1                   # optional, defaults to 1
model: sotif                 # kpi_config/models/<kpi_group>/sotif.yaml (group is authoring only)

time:
  column: event_month        # date column on the extract
  grain: month               # day | week | month | quarter | year
  filter_code: reporting_month   # optional scalar; wins if present on the context
  periods:                   # independent year/quarter/month/week/day predicates
    year: year
    month: "current month"
  calendar: gregorian        # gregorian | fiscal
  fiscal_start_month: 4      # only read when calendar: fiscal

parameters:                  # optional; omit when the KPI has none
  time_grain: { type: string }   # reserved overlay: pick from time.grains
  output_cut: { type: string, default: G, allowed: [G, R] }  # walk root; YAML default does not lock
  Level: { type: string, allowed: [G, Y, R], map: { Green: G } }

dimensions:
  - { name: reason_code, from: reason_code }
  - { name: region, from: region }
  - { name: supplier, from: supplier_name }

default_dimensions: [reason_code]   # required; [] is worldwide

base_measures:               # internal facts; the UI does not request these
  sotif_value:
    sql: amount              # physical column (or expr: for nested + - * /)
    agg: sum                 # Pandas folds the retrieved rows
                             # or: columns: [ontime, fullqty] + op: multiply

cuts:                        # group_by is extras only (not the full grain)
  - name: G
    group_by: []
    exclude_from_grain: [region]
    ignore_filters: [region]
    also_emit: [R]
  - name: R
    group_by: [region]
    ignore_filters: []

default_cut: G
row_set: span_union          # span_union | anchor_only

# How context filters land (values still come from the request). Omit = IN at extract
# unless an emitted cut ignore_filters the code (then calc). All row filters skip when
# omitted or []. optional: true is accepted and ignored; optional: false is a bind error.
# filters:
#   effective_day: { column: day, op: lte, apply: extract }

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
| A formula over retrieved columns | `expr:` on a base measure (see §4) — named steps, `+ - * /`, CASE, allowlisted calls |
| A static map (payment fee, tier rebate) | `lookup:` on a base measure (see §4.1) |
| Entity window (sequence, days-since, running sum) | `over:` on a **pre-fold** base (see §4.2). Not calendar `op: lag` |
| Drop groups below a measure floor | KPI `having:` (see §5.8). `op: predicate` flags 1/0 without dropping |
| Share of **surviving** groups after a floor | `having.then_group_by` then `op: percent_of_total` |
| A formula over other measures | `op: expr` (see §5.6) |
| A named function over other measures' values | `op: fn` + `inputs:` (see §5.5) |
| A named function over retrieved columns | `columns:` + `op:` on a base measure (see §10.1) |
| A dimension echoed as a `measure_key` | `op: dimension` |
| Quartiles, Pareto, lag, vs-target, series stats | add-on ops / hooks — [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) |
| Math no listed kind can express | new hook under `capabilities/hooks/` + `registries/hooks.yaml` (see §10.3) |

---

## 2. How a request is calculated

Understanding this makes the YAML obvious.

1. Time parts on the context are independent predicates on the period column. Together they define a **selection** S of `time.grain` buckets. `anchor = max(S)` (calendar end, not last observed). A scalar `time.filter_code` on the context still wins and is one bucket. Missing parts are simply not applied; no time filters at all means whole history (the engine probes min/max from data).
2. The engine reads the **requested** `measure_key`s, works out the deepest lookback among them, and scans `[span_start, anchor + 1)` as a **date range**. Point measures fold every observed bucket in S; windows and trends measure from `anchor`. Measures nobody asked for cost nothing.
3. DuckDB retrieves **model columns only** (time bucket, dimensions, and physical columns the row pipeline walks to). `filters:` with `apply: extract` become the extract `WHERE` (plus undeclared context `IN` lists). It does not run `agg:`, `op:`, named `expr:` steps, `lookup:`, or `over:`. A `kind: sql` model may still shape **this extract** (joins, filters, optional SQL math).
4. After retrieve, the engine stable-sorts detail (time, grain dims, `over.order_by`) and assigns `_kpi_row_id`. Pandas then topo-sorts `base_measures` (helpers, `lookup:`, `expr:`, `over:`), folds bases that declare `agg:`, densifies, and computes combo measures (point / window / trend / `expr` / `fn` / `op: predicate`).
5. Each cut re-aggregates that spine to its **effective group_by**. **HAVING** drops groups by measure predicates (per cut), optionally `then_group_by` re-folds survivors from filtered facts, then **all** cut-phase ops (`rank`, `percent_of_total`, …) run on what remains. `apply: result` is dim-only and runs after that. `green_when` stamps remaining rows; pagination `total` is remaining rows. Result filters on dims not in that cut’s grain skip (`not_in_grain`).

The practical consequences:

- A missing month stays missing. "Same month last year" cannot silently pick up the row that happens to sit 12 rows back.
- One request can return several grains (`also_emit`) from a single scan. `parameters.output_cut` (when sent on the context) is the walk **root**, not a hard lock; set `pack_also_emit: false` on that cut to emit only that grain.
- Adding a measure to YAML does not widen the scan until someone requests it.

---

## 3. `time` — grain, calendar, anchor

| Key | Values | Default | Notes |
|---|---|---|---|
| `column` | column name | required if `time:` is present | Date or timestamp on the extract |
| `grain` | `day`, `week`, `month`, `quarter`, `year` | `month` | Default period every measure counts in. `week` is ISO Monday. |
| `source_grain` | same set as `grain` | `time.grain` | Stored grain of the time column. A request pick **finer** than this is rejected (day < week < month < quarter < year). Monthly facts cannot become daily or weekly. |
| `grains` | list of grains | omitted = `{grain}` only | Allowlist for `parameters.time_grain`. `time.grain` must appear in the list. |
| `filter_code` | context filter key | required unless `periods:` or `compose:` is set | Case- and space-insensitive (`Reporting Month` matches `reporting_month`). **Not** hardcoded to `reporting_month`. When present on the context this scalar **wins** outright (legacy single-bucket); declared `periods` parts are skipped. When `compose:` is set, this is the **synthetic** name after concat. |
| `periods` | map of part → context filter code | omitted | Independent predicates: `year`, `quarter` (1–4), `month` (1–12), `week` (ISO 1–53), `day` (1–31). Values parse as integers (`3`, `"03"`). The **month** part also accepts English names and 3-letter abbreviations (`March`, `Mar`, case-insensitive). Lists are a union. A missing part is not applied. Mutually exclusive with `compose.template`. A part finer than `time.grain` is a bind error. |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal affects `quarter` and `year` only (`year` / `quarter` parts use the fiscal calendar). `week` is ISO-only. |
| `fiscal_start_month` | 1–12 | `4` | First month of the fiscal year |
| `format` | `yyyy-mm-dd`, `yyyy-mm`, `yyyy/mm`, `yyyymmdd`, `yyyymm`, `mmyyyy`, or a strptime string | ISO `YYYY-MM` / `YYYY-MM-DD` | How the physical column and the context time filter are stored (`062026` → `format: mmyyyy`) |
| `compose` | `{ template: "{year}{month:02}" }` | omitted | **Superseded by `periods:`.** Build `filter_code` from segregated context keys. Literals between `{placeholders}` are kept (`{year}/{month:02}` → `2026/04`). `{month:02}` zero-pads. The part keys are then removed so they are not leftover `IN` filters. If `filter_code` is already on the context, that scalar wins. Cannot be set together with `periods:`. |
| `anchor` | `selection_end`, `last_observed` | `selection_end` | `selection_end` is `max(S)`. `last_observed` probes the extract and sets `anchor = min(selection end, last observed bucket)`. `validate()` reports `anchor: null` and `anchor_source: "data"` in this mode. Suppresses the `unobserved_anchor` note. |
| `max_span_years` | positive int | omitted | After the span is final, a span longer than this many years is `TimePlanError` (narrow the time filters). Uncapped KPIs still get a `wide_time_span` note above 10 years. |

Omit the entire `time:` block when the KPI has no period column. The engine then aggregates the filtered extract as a snapshot: no month filter, no date range, no dense spine. Leftover host period filters (`reporting_month`, `month`, `year`, `as_of_period`, and compose year-month keys) are skipped with `reason: no_time` even if mapped — they are not IN-filters on a snapshot. Other unmapped valued filters stay FilterError. A snapshot measure may not use a nonzero `offset`, `trailing`, or any kind that needs time (`window`, `trend`, `lag`, period hooks, …). `point` + `offset: { months: 0 }` is allowed. `constant` + `trailing` is not. Snapshot KPIs may still use `over:` with a non-time `order_by`.

Rules the engine enforces:

- Declared `periods` parts conjoin. Year alone is the full year; year + month is that month; month alone is every matching month across years (anchor = latest). Part values may be lists (`month: [1, 2, 3]`). Month names (`March`, `Mar`) are accepted for the month part only. Garbage (`month: "banana"`) still raises `TimePlanError`; only **absence** is lenient.
- A scalar `time.filter_code` on the context is still **exactly one** value (two values is an error, not a range). That path is byte-identical to today's single-bucket KPIs.
- Missing time filters on a time-based KPI are legal: the engine probes min/max under the part predicates and bound dimension filters. `validate()` reports `anchor: null` and `anchor_source: "data"` until compute fills them. An empty selection (impossible combo, or no matching data) sets `anchor` to null, returns null measures / empty trend axis, and adds a `notes` entry — it does not raise.
- Point measures fold the observed buckets of S. An offset shifts **each** bucket (`years: 1` on a year-only 2026 selection is the full 2025 total). Windows and named ranges (`mtd`, `ytd`) measure from `anchor` (`trailing: 3` from June is Apr–Jun; `mtd` under `year=2026` is December).
- `parameters.time_grain` (context.parameters, not a filter, not `execution`) picks one allowlisted grain. The KPI must declare `parameters.time_grain`. Missing on the context → YAML `default` or `time.grain`. Not in `time.grains` (or not equal to `grain` when `grains` is omitted) → bind error. After bind, plan / DuckDB bucket / densify / `trailing.periods` use the pick, then part predicates apply at that grain. Response `parameters.time_grain` is the effective grain; bound request values are `request_parameters`.
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

When the host sends independent year / quarter / month (and optionally week / day) filters, declare `time.periods`. Only those codes are read; they are not leftover `IN` filters. `parameters.time_grain` re-buckets first, then parts apply at the effective grain.

```yaml
time:
  column: event_month
  grain: month
  calendar: gregorian
  filter_code: reporting_month     # optional; still wins if present on the context
  periods:
    year: "year"
    quarter: "quarter"
    month: "current month"
```

`year=2026` alone folds Jan–Dec 2026 (`anchor` = Dec 2026). `year=2026` + `month=6` is June only. `month=3` with no year is every March in the data.

When the host instead concatenates year + month into one period key, `compose` still works (`time.format` must parse the **result**). Lookback still widens from requested measures. Prefer `periods:` for new KPIs.

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

### Request parameters (`context.parameters`)

Calculation controls are **not** filters and **not** `execution.*`. Send them as a top-level `parameters` object, sibling of `filters`. Declare them on the KPI.

There are **four** overlays (not one grammar). Do not add a fifth (`select:`, `use:`, `{ from: param }`). **`selected_dimensions` is not a parameter.** Send it as top-level `context.selected_dimensions`. Declaring `parameters.selected_dimensions` is a bind error.

1. Reserved `parameters.time_grain` — pick from `time.grains` (feeds `apply_request_time`).
2. Reserved `parameters.output_cut` — start the `also_emit` walk at that cut. A YAML `default` is **not** a lock (G still packs R). Sending the parameter on the context is. Set `cuts[].pack_also_emit: false` to emit only that root. 3004 does **not** declare this. Hosts that want one grain send `output_cut` on the request.
3. `when:` on `model`, `measures.<key>`, or `base_measures.<name>` only.
4. `from_param:` on an allowlist (see below). Never the YAML key `from` (that stays `trailing.from: data_points` / `dimension.from`).

### Request grain (`context.selected_dimensions`)

Omitted / `null` → YAML `default_dimensions`. `[]` / `{}` → empty grain (grand total, then cut extras). Array or `{ "names": [...] }` keeps host order. Object of bools uses catalog YAML order. Unknown names (including `false` keys) and empty strings are bind errors.

```json
"selected_dimensions": ["supplier", "region"]
"selected_dimensions": { "names": ["supplier", "region"] }
"selected_dimensions": { "supplier": true, "region": true }
```

```yaml
parameters:
  time_grain:                 # reserved overlay 1
    type: string              # string | int | float | bool | list | dict
    # default omitted → time.grain
    # allowed omitted → time.grains
  output_cut:                 # reserved overlay 2
    type: string
    default: G
    allowed: [G, Y, R]
  Level:                      # overlay 3 (when:) and/or scalar inject into expr
    type: string
    allowed: [G, Y, R]
    map: { Green: G, Yellow: Y, Red: R }
  lookback: { type: int, default: 3 }          # overlay 4 (from_param:)
  codes: { type: list, item: string, default: [G, Y] }
  flags: { type: dict, item: string }          # echoed on request_parameters only
```

`when:` shape (metadata is not mixed with case labels):

```yaml
measures:
  sotif_value:
    when:
      param: Level
      cases:
        G: { op: expr, expr: g_amt }
        Y: { op: expr, expr: y_amt }
      else: { op: expr, expr: r_amt }   # always required
```

Same shape on `base_measures.*` (each case is a base body) and `model` (each case / else is a model-id **string**). Match is `str(bound)` vs `str(case_key)` after `map:` / `allowed`. `else:` is always required. Case keys must be in `allowed` when `allowed:` is set. `param` / `cases` / `else` are not valid case labels.

`from_param:` is `{ from_param: <declared param> }` only, after `when:` pick:

- `model` — param type `string` (model id). Context datasets still match **aliases**, not model id.
- `measures.*.trailing.{months,weeks,days,quarters,years,periods}` — `int`
- `measures.*.offset.{months,weeks,days,quarters,years}` — `int`
- `measures.*.value` only when `op`/`kind` is `constant` — `int` or `float`. Not hook `value:`.

A slot is `when:` **or** `from_param:` **or** a concrete value, not two. Nested `when:` is an error. `from_param:` inside a picked case body is allowed.

Complete-pack validation: when every `when.param` in the file is the same name, the binder materializes and parses **every** `allowed` value (and `else`), including column checks against the chosen model's `output_schema`. Two different `when.param` names are checked per slot plus the live request combination (no cartesian product). Top-level templated `model` plus any `base_measures.*.model` is a bind error (per-base model is not moved by the KPI default switch).

`type: list` needs `item: string|int|float|bool`. `allowed` / `map` apply per element. Empty list is legal. List params may appear in expr **only** as the right of `in` (`Level in codes` or `Level in ('G', 'Y')`). `type: dict` is string keys / scalar values, echoed on `request_parameters` only — not in expr, not merged as fn extras. Expr still uses `=` (not `==`).

`load_kpi(id, parameters=)` binds the request (or `{}`) **before** resolve. There is no later bind that re-picks `when:` bodies. Tests that need `Level=Y` must pass `parameters={"Level": "Y"}` (or use `compute` / `validate`).

Rules:

- `execution` is identity only (`kpi_id`, `view_details`, `request_id`). Leftover `execution.time_grain` is a bind error.
- Filters stay source columns. `Interval` / `Level` left in `filters` fail as unmapped columns.
- A KPI with no `parameters:` block rejects a non-empty `context.parameters` (3004). Omit the object or send `{}`.
- Unknown keys, missing keys (no default), and values not in `allowed` are bind errors.
- Parameter names must not equal measure keys (`when:` may not add or remove measure **keys**, only bodies).
- Response `parameters` is the time plan (`anchor`, `time_grain`, `span_start`, `lookback_months`, `time_selection`). `time_selection` is `{grain, start, end, parts, anchor_source}` where `anchor_source` is `context`, `data`, or `legacy`. Bound request values are `request_parameters`. Time parts appear on `applied_filters` as calc-stage. Result/trend rows keep their per-bucket `period`; only the headline carries the selection.

```json
{
  "execution": { "kpi_id": "3004", "view_details": ["..."], "request_id": "..." },
  "parameters": { "time_grain": "week", "output_cut": "G", "Level": "Green" },
  "filters": { "time": ["2024"], "region": ["EMEA"] }
}
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
      op: in           # in | eq | ne | gt | gte | lt | lte | between
      values: [O]
```

`eq` / `ne` / `gt` / `gte` / `lt` / `lte` also accept `value:` (singular). `between` requires `values: [lo, hi]` (exactly two). Numeric compare ops coerce the column; `in`/`eq`/`ne` do not (status codes stay strings). `ne` is SQL-style: nulls do not pass. `like` / `is_null` are not legal on base-measure `where:`. `first`/`last` on a text column still coerce to numeric (unlike `count` / `count_distinct`).

`sql: amount` + `agg: sum` names the physical column DuckDB retrieves. Pandas then sums it. KPI YAML formulas never appear in the DuckDB SQL.

Prefer `expr:` for nested arithmetic, or `columns:` + `op:` for a registered function. Do **not** desugar `arithmetic` / `fn` / `columns`+`op` into `expr` — those kinds stay for plugins and existing YAML. The preferred authoring path for new KPIs is named `expr:` steps (and `lookup:` / `over:`).

`expr:` / `lookup:` / `over:` without `agg:` are **row helpers**: they exist only in the row pipeline and are not densify-filled. They cannot be `measures.of` unless the KPI sets `identity_grain:` (a subset of `dimensions`) and **every emitted cut's** effective grain equals that set (point, offset 0 only). Duplicate identity tuples are CatalogError. Omit `identity_grain` (or emit a coarser `also_emit` cut) → BindError; fold with `agg: first|last|max` instead. Calendar `window` / `lag` of a helper stay illegal. Omit `agg` on `sql:` / `columns:`+`op` and it still defaults to `sum`.

A later `expr:` may name an earlier base. Cycles are BindError. If an extract column listed on `datasets[].columns` or model `output_schema` and a YAML `expr:` / `over:` / `lookup:` share a name: BindError, unless the base sets `replace: true` (Pandas overwrites and `grain_warnings` records `replace_extract_column`). If the host lists **no** columns and the name is not in `output_schema`, `validate()` cannot see the clash; compute still CatalogError.

`sql:` / `expr:` name physical columns **or earlier helpers** (and optional `+ - * /`, CASE, or allowlisted calls including `date_diff` / `date_add` / `epoch_day` and `round` / `floor` / `ceil` / `power` / `log` / `log10` / `sqrt`). SQL `SUM()` / subqueries are rejected in KPI `sql:`; put the aggregation in `agg:`. DuckDB only SELECTs the physical column names; Pandas evaluates the formula. A `kind: sql` model may still contain `SUM(` / `LAG(` to shape **this extract**.

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
| `round` / `floor` / `ceil` | 1 (+ optional decimals) | Domain-safe rounding |
| `power` / `log` / `log10` / `sqrt` | 1 or 2 | Domain errors (log/sqrt of negative, 0^negative) → null |
| `date_diff` / `date_add` / `epoch_day` | dates | tz-naive only; units `day` / `week` / `month` / `year` |

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

Do **not** use `agg: sum` on a per-row ratio if you wanted a ratio of two totals. Declare two base measures and `op: expr` (or `op: arithmetic`) instead.

### 4.1 `lookup:` — static maps

```yaml
platform_fee:
  lookup: { column: payment_method, map: { COD: 25 }, default: 10 }
rebate_pct:
  lookup: { column: customer_tier, map: { Gold: 0.05, Silver: 0.02 }, strict: true }
```

Keys are compared after `str()` + strip. Unknown → `default` if set, else null. `strict: true` cannot combine with `default:`; any unknown key at eval is CatalogError. Mutually exclusive with `expr:` / `over:` / `sql:` / `columns:`+`op` on the same base.

### 4.2 `over:` — entity windows on pre-fold rows

Calendar `op: lag` stays on the densified spine vs the **anchor**. Entity sequence / days-since / running totals use `over:` on the **detail frame before fold**. `over:` is illegal on a combo measure. `op: lag` with `partition_by` is BindError.

```yaml
order_seq:
  over: { fn: row_number, partition_by: [customer_id], order_by: [order_date, order_id] }
  agg: max
running_final:
  over: { fn: running_sum, of: final_amount, partition_by: [customer_id], order_by: [order_date, order_id] }
  agg: max
```

`fn`: `lag` / `lead` / `row_number` / `rank` / `dense_rank` / `running_sum` / `running_avg` / `last_n`. `order_by` is required (≥1 column). Window sort is `order_by` then `_kpi_row_id`; nulls sort last. `over.of` is required for `running_*` / `last_n` / `lag` / `lead` of a value (it does not default to this helper's name). Rank fns stay of-optional. `last_n` writes a JSON list of the last n `of` values (dates as ISO strings); fold only with `first`/`last` (or `agg_ok`). `last_n` may sit on `op: point`. It cannot be `of`/`inputs` of window, arithmetic, fn, predicate, having, cut ops, `green_when`, threshold, trend, or numeric hooks.

Caps: `OVER_ROW_CAP` (500,000 detail rows) and `OVER_PARTITION_CAP` (50,000 distinct partition tuples). Exceed → CatalogError; no silent truncate.

`agg: sum|avg|count|count_distinct` on a window is BindError unless `agg_ok: true` (computes + `grain_warnings` `window_agg_ok`). Allowed identity aggs: `first|last|min|max`. Densify / `fill_zero` never feed `over:`. Snapshot KPIs may use `over:` with a non-time `order_by`. Two-model KPIs: `partition_by` / `order_by` / `lookup.column` must be on **that** model's retrieve (BindError if they name a base on the other extract). Combo `op: expr` / arithmetic / fn may only name other **measures**, not a row helper (BindError). Helper as `op: point` `of` requires `identity_grain` (see above).


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
| `stddev` | Pandas over raw rows | recomputing from rows | `null` (also `null` on a single row) |
| `variance` | Pandas over raw rows | recomputing from rows | `null` (also `null` on a single row) |
| `mode` | Pandas over raw rows | recomputing from rows | `null` (ties → smallest) |

Three behaviours worth knowing before you pick an aggregation:

**`avg` is weighted, never an average of averages.** It travels as `SUM` and `COUNT` and divides at the very end. A 3-month average over months of 2, 1 and 1 rows is `total / 4`, not `(a + b + c) / 3`.

**`min` and `max` are recomputed at every cut.** A global cut takes the minimum across regions; it does not add regional minima together.

**`count_distinct`, `median`, `percentile`, `first`, `last`, `stddev`, `variance` and `mode` are non-additive**, so Pandas keeps the retrieved rows and recomputes over the window. `stddev` / `variance` are the sample (ddof=1) statistics; a single row is `null`. `mode` is the first of pandas `Series.mode()` after numeric coerce (ties → smallest). `last` on a balance is the latest snapshot in the period — it does not add daily balances. This costs more memory than an additive measure; keep the dimensionality sensible.

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

Platform kinds are documented below. Add-on kinds (`ntile`, `lag`, `diff`, `top_n`, …) and hooks (`ewma`, `hit_rate`, `cagr`, `mad`, `projection`, `period_median` / alias `rolling_median`, …) are listed with examples in [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md). A new name is `capabilities/` + `registries/` — not `pipeline/`.

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

`offset` counts **backwards** from the anchor. Available units: `days`, `weeks`, `months`, `quarters`, `years`; they add together, so `{ years: 1, months: 2 }` is 14 months back. `offset.weeks` is a calendar 7-day shift (also valid on `lag` / `lead` / `index` / `diff` / `pct_change`). Month-end dates clamp (31 Mar − 1 month = 28/29 Feb).

Returns `null` when that period has no rows for this dimension combination.

Opt-in **per-measure filters** (CALCULATE-lite) on a point (or any measure whose `of:` is a **base**):

```yaml
late_only:
  of: sotif_value
  op: point
  where: { column: reason_code, op: eq, value: LATE_SUPPLIER }

worldwide:
  of: sotif_value
  op: point
  ignore_filters: [region]
```

`where:` is the same shape as `base_measures.where:` (AND-merged onto a hidden clone of that base). `ignore_filters:` lists context filter codes this one measure skips (reported as `ignored_filters` reason `measure_<key>`). Both keys are bind errors when `of:` is another measure — filter the base instead. Hooks may be lagged (`lag { of: smoothed }`); they evaluate at the shifted anchor.

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

`offset` on a window shifts the reference **backwards**, then the range is applied. Named PTD / `full_*` ranges stay calendar and do not change meaning when the grain pick changes (`qtd` is still quarter-to-date; month + `qtd` is valid). `wtd` binds when `day` is allowed and fails at evaluate unless the pick is `day`. Named ranges cannot also set `trailing:`. `inclusive` applies only to trailing/leading. Fiscal vs calendar follows `time.calendar`, except a host **year part** forces calendar Jan–Dec for `ytd` / `full_year` / year grain.

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

- Returns a list of `{period, value}` objects, one per grain step. The shared x-axis is still in `trend_axes` (ISO period starts) and `trend_labels` (fixed English: `23 Mar`, `2026-W30`, `Jul 2026`, `2026-Q1`, `2026`). Same keys and lengths. Labels are unique enough for a chart and are not locale-dependent.
- A period with no rows keeps its slot: `0` for `sum`/`count`, `null` for everything else.
- Trends are emitted **only on the default cut** unless `cuts:` lists more. This is deliberate — a trend on a high-cardinality cut multiplies the payload.
- Guardrail: rows × array length may not exceed **50,000 cells** per cut, otherwise the request fails and asks you to narrow `cuts`.
- `offset:` on a trend shifts the axis window (last year's 12-month trend is `op: trend` + `offset: { years: 1 }`). `lag { of: trend }` stays a bind error; put `offset:` on the trend.

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

To rank last period's value, rank a lagged measure (`rank { of: lagged }`). `lag { of: rank }` is a bind error.

`order_by: [order_date]` on cut ops (`rank`, `row_number`, `running_total`, `percent_of_total`, …) sorts groups by those dim columns **after** the existing `of` measure order. Default remains sort-by-measure. Final tiebreak: original combo index.


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
| `versus_cut` | Optional. Divide by another **declared** cut's total of `of` instead of this cut's partition sum. The target is computed even if that cut is not emitted. Cycles are bind errors. |
| `measures.*.cuts` | Emit this column on these cuts (default: `default_cut` only) |

```yaml
percent_within_site:
  op: percent_of_total
  of: current_value
  partition_by: [site_category]
  cuts: [R]
```

Null source → null. Zero or null total → null (never `inf`). Scale is 0–100. Same two-phase pass as `rank`. `arithmetic` / `fn` / `expr` may consume rank and `percent_of_total` (a post-cut derived pass). Trends and hooks cannot. `group_by:` is accepted as an alias for `partition_by`.

```yaml
share_of_global:
  op: percent_of_total
  of: current_value
  versus_cut: G
  cuts: [R]
```

### 5.3d `having:` — drop groups by measure predicates

KPI-level (not a measure). Runs **per cut** after combo scalars exist and **before** cut ops, so rank/share never include dropped groups. Empty survivors → zero rows for that cut (not BindError). `dropped_groups` lists `{ cut, reason: "having", key: { dim: value, ... } }` (dim keys only; cap is `OVER_PARTITION_CAP`).

```yaml
having:
  match: all                 # all (default) or any
  predicates:
    - { of: total_profit, cmp: gt, value: 0 }
    - { of: return_rate, cmp: lt, value: 0.20 }
  then_group_by: [product_category]   # optional
```

`cmp`: `gt|gte|lt|lte|eq|ne|between` (`between` needs inclusive `low`/`high`). Literal `value:` or `vs:` another **scalar** measure. Null `of` fails the predicate. Trend-array `of` is BindError. `of:` keys are auto-included like `green_when.of`. Worldwide rate ≠ regional rate: each cut (including `also_emit`) gets its own having + rollup.

`then_group_by` must be a subset of that cut’s **pre-having** effective grain (empty list = one worldwide survivor row). Survivors are identified by fine-grain keys → filter `cut_monthly` / `cut_detail` → re-collapse to `then_group_by` → re-run combo then cut ops. Do **not** sum already-computed avgs/margins.

HAVING sees the same combo scalars the client would have seen (post densify/`fill_zero`). Zero-filled sparse groups can fail `gt: 0`. `apply: result` stays dim-only and runs **after** having/rollup/cut ops.

### 5.3e `predicate` — 1/0 flag (does not drop)

```yaml
healthy:
  op: predicate
  match: all
  predicates:
    - { of: total_profit, cmp: gt, value: 0 }
    - { of: return_rate, cmp: lt, value: 0.20 }
```

Combo-phase. Same `cmp` set as `having:`. Can itself be `having.of`. Use this to flag; use `having:` to filter.

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

Cut-phase (`ntile`, `dense_rank`, `row_number`, `percent_rank`, `cumulative_share`, `running_total`, `running_avg`, `contribution`, `gap_to_leader`, `gap_to_avg`, `zscore`, `top_n`) and period-phase (`lag`, `lead`, `index`, `vs_target`, `threshold`, `diff`, `pct_change`) ops are allowlisted add-ons. YAML keys are kind-specific (`tiles`, `n`, `vs`, `cmp`, `offset`). `lag` / `lead` / `index` / `diff` / `pct_change` may `of:` a base or a shiftable measure (`point`, `window`, `fn`, `expr`, …). They cannot shift `trend`, `hook`, `rank`, or a row helper (`agg` omitted). Copy the example from [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md). Do not invent a name that is not in the registry.

---

## 6. `cuts` — grouping grains in one response

A cut is a grouping **variant**, not a number and not a formula. `group_by` lists **extras** only. Effective grain = request grain minus `exclude_from_grain`, then extras. One request can return several cuts.

```yaml
default_dimensions: [reason_code]
cuts:
  - name: G
    group_by: []
    exclude_from_grain: [region]
    ignore_filters: [region]
    also_emit: [R]
    pack_also_emit: true     # false = emit only this cut when it is the walk root
  - name: R
    group_by: [region]
    ignore_filters: []

default_cut: G
```

| Key | Meaning |
|---|---|
| `name` | Appears as `output_cut` on every row |
| `group_by` | Extra dimensions this cut always adds (must not overlap `default_dimensions`) |
| `exclude_from_grain` | Request dims this cut drops. Dim-named `ignore_filters` must match this list both ways |
| `ignore_filters` | Filter codes or column names this cut ignores |
| `also_emit` | Other cuts to return in the same response (chains are followed, cycles are safe) |
| `pack_also_emit` | Default `true`. When `false`, a walk that starts at this cut does not follow `also_emit` |
| `default_cut` | The cut the walk starts from when `output_cut` is not on the context; defaults to the first declared cut |
| `default_dimensions` | Grain when `selected_dimensions` is omitted. Required. `[]` is worldwide |
| `identity_grain` | Optional. Subset of `dimensions`. Helpers may be `op: point` `of` only when every emitted cut equals this grain |

**How `ignore_filters` works.** A filter ignored by *any* **emitted** cut is kept out of the DuckDB `WHERE` clause and applied per-cut in Pandas (`apply: calc`). That is what lets `region=NA` narrow the R rows while G still reports worldwide from the same scan. Default `apply: extract` is legal with `ignore_filters`; at request time `split_filters` promotes it to calc when that cut is emitted. Sending `parameters.output_cut: G` still walks `also_emit` unless that cut sets `pack_also_emit: false`. If only R is emitted (`output_cut: R`, or G with `pack_also_emit: false`), region can stay extract. Do not combine `ignore_filters` with `apply: result`. The response reports where each filter ran under `applied_filters`, cut-level skips under `ignored_filters`, and present-but-blank keys under `skipped_filters`. A dim-named ignore token must also appear in `exclude_from_grain`, and the reverse.

Every cut re-aggregates the spine from scratch, so a global average is a true weighted average, not a mean of regional averages. Non-additive ops (median, percentile, first, last, count_distinct) evaluate on fact rows at the cut keys, not on rolled sums.

### `row_set` — which combinations get a row

| Value | Emits a row for a dimension combination when… |
|---|---|
| `span_union` (default) | it has data **anywhere in the scanned span** |
| `anchor_only` | it has data **at the selected period** |

Use `anchor_only` when the page should list only what is active now; use `span_union` when a combination that dropped to zero this month should still show its history.

---

## 7. Filters

YEAR / MONTH belong on `time.periods` (independent predicates) or on `time.filter_code` (one selected period → a date range plus lookback). `time.compose.template` still concatenates segregated keys (`year` + `month` → `202607` / `2026/04`) but is superseded by `periods:`. Do not also leave year and month as leftover `IN` filters — both `periods` and `compose` strip them.

Other predicates are **row filters**. Values come from the **context**. KPI YAML `filters:` says **how** and **at which of three stages**.

```yaml
filters:
  effective_day:
    column: day
    op: lte                 # or "<="; aliases are case-insensitive
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

`ignore_filters` is not a fourth apply. It names which **emitted** cuts skip a calc-stage filter. Default `apply: extract` plus `ignore_filters` is legal (runtime defers when that cut is emitted). Do not combine it with `apply: result`.

Undeclared context codes stay `IN` at extract, unless an emitted cut lists them in `ignore_filters` (then calc). `filter_map` still remaps a code to a column (`op: in`, `apply: extract`).

**All row filters are non-binding (breaking vs empty `IN` = FALSE).** Omit the key, send `[]`, or send all-null comparison values → skip; the key appears on `skipped_filters` when it was present but blank. `[""]`, `["ALL"]`, `["*"]` are real predicates. `optional: true` is accepted and ignored. `optional: false` is a bind error. `is_null` / `is_not_null` apply when the key is **present** (omit the key to skip). Row-filter `compose` with a missing or blank part skips; time `compose` still errors if a placeholder is missing. A scalar `time.filter_code` still requires exactly one value when the key is present. Missing period parts are not applied.

`apply: result` may name **dimension** columns on the output row only, not measure keys. Dimensions not in a cut's effective grain are omitted unless they are rolled up (`null` sentinel). A result `IN` on fields outside the grain is skipped (`not_in_grain`) rather than hiding the cut by matching a stamped null. G worldwide is `default_dimensions: [reason_code]` plus `exclude_from_grain: [region]` / `ignore_filters: [region]`; `selected_dimensions: []` makes G's grain empty.

### Operators (all three `apply` stages)

| Canonical | YAML aliases | Values |
|---|---|---|
| `in` | `IN` (default if `op` omitted) | 0+ (empty / omitted → skip) |
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
| Comparison other than `IN`, or a chosen `apply` | Add `filters:` |

```yaml
filter_map:
  plant_code: region      # context filter_code → column name (must be an identifier)
```

`filter_map` takes precedence over context mappings for this KPI. `filters:` `column:` wins over both for that code.

Contract details:

- An unmapped filter **with values** is a **hard error**, never silently dropped. An unmapped blank `[]` is skipped (`skipped_filters`).
- An empty `in` list, omitted key, or all-null values **skip** the predicate (not `FALSE`). This is a breaking change from matching nothing.
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

## 9. Model YAML (`kpi_config/models/<kpi_group>/<model_id>.yaml`)

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

Prefer this when the extract needs anything a simple join cannot express. DuckDB SELECT is grain + walked physical columns (not the full parquet column list). A walked name missing from `output_schema` is BindError; the dump skip does not invent CTE columns.

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

A new name never requires an engine change. Add the Python under `capabilities/` and a row under `registries/`. Then regenerate `registries/CAPABILITIES.md`. Both registries are validated at bind time, so a typo names the registered alternatives instead of failing mid-request. See [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md).

This registry does **not** cover filter operators, compose templates, time format aliases, or aggregations — those stay platform code in `pipeline/`.

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
# kpi_engine/capabilities/hooks/blend.py
def blend_mom(series, *, kpi, plan, spec, **_):
    """0.5 × current period + 0.5 × previous period."""
    current = _at(series, kpi.time.column, spec.of, plan.anchor)
    prior = _at(series, kpi.time.column, spec.of, add_months(plan.anchor, -1))
    if current is None or prior is None:
        return None
    return 0.5 * current + 0.5 * prior
```

Then allowlist it in `registries/hooks.yaml` (`module` / `attr`, plus `requires_value` or `extra_keys` if needed). Do not edit `pipeline/`.

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
  - { name: reason_code, from: reason_code }
  - { name: region, from: region }
base_measures:
  sotif_value: { sql: amount, agg: sum }
default_dimensions: [reason_code]
cuts:
  - { name: G, group_by: [], exclude_from_grain: [region], ignore_filters: [region], also_emit: [R] }
  - { name: R, group_by: [region], ignore_filters: [] }
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

### D. Independent year / month filters

```yaml
time:
  column: event_month
  grain: month
  periods:
    year: year
    quarter: quarter
    month: "current month"
```

Point `current_value` folds every selected month. `previous_year_value` with `offset: { years: 1 }` shifts each selected month. A trailing window still starts at `anchor` (December when only `year=2026` is sent).

### E. Ratio across two models

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
| `Cannot parse month 'banana'` | Time part was not an integer or month name | Send `3`, `"03"`, `March`, or `Mar` |
| `time.max_span_years` | Densified span exceeds the KPI cap | Narrow the time filters or raise the cap |
| `time.anchor must be` | Unknown `time.anchor` | Use `selection_end` or `last_observed` |
| `time.periods.week is finer than time.grain` | Part is finer than the KPI grain | Drop that part, or lower `time.grain` |
| `time.periods and time.compose.template cannot both be set` | Mutually exclusive | Keep `periods:` (preferred) or `compose:`, not both |
| `Compose placeholder 'year' is missing` | A `{placeholder}` was not on the context | Send that filter, or send the composed `filter_code` as one scalar |
| `Month filter must contain exactly one value` | The page sent a multi-select for scalar `filter_code` | Metadata — the legacy anchor is a single period; use `periods:` for lists |
| `time.grain=day requires a full date` | Day-grain KPI received `YYYY-MM` | Send `YYYY-MM-DD` |
| `Filter '<x>' has no column mapping` | No context mapping and no matching column | Add `filter_column_mappings` or `filter_map` |
| `Unknown filter op '…'` | YAML `filters.*.op` is not in the operator table | Use a name or alias from §7 |
| `Filter '…' op 'between' expects 2 value(s)` | Wrong arity | `between` needs `[low, high]`; `eq` needs one value; `is_null` needs none |
| `filters.x apply: result cannot be listed in ignore_filters` | Hiding JSON rows is not a per-cut skip | Use `apply: calc` + `ignore_filters` |
| `filters.x.optional: false is not supported` | Row filters cannot be required | Omit `optional:`, or omit/[] the key at request time |
| `Filter '<x>' does not bind to a source column` | Mapped to a column the extract does not expose, or a control sent as a filter | Add it to `output_schema`, or declare YAML `parameters:` and send `context.parameters` |
| `execution.time_grain is not supported` | Grain pick was on `execution` | Send `parameters.time_grain` |
| `This KPI declares no parameters` | `context.parameters` was non-empty on a KPI with no schema | Omit it, or add YAML `parameters:` |
| `Unknown parameter(s)` / `Missing required parameter` / `not allowed` | Schema mismatch | Match YAML `parameters:` names, defaults, and `allowed` |
| `Illegal measure sql: '…'` | Comments, `;`, double quotes, or incomplete CASE | Use `+ - * /`, CASE, or an allowlisted call; put `SUM` in `agg:` |
| `names unknown function` | Call is not in that layer's registry | Use a name from [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) |
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
| `is a row helper` | Helper used as `of` without identity grain, or on a coarser cut | Set `identity_grain` and emit only that grain, or add `agg:` |
| `last_n is a JSON list` | Numeric op consumed `last_n` | Keep `last_n` on `op: point` |
| `would overwrite extract column` | Writing helper name listed on datasets/output_schema | Rename, or `replace: true` |
| `does not project` | `kind: sql` walked column not in the CTE | Expose it on the SQL SELECT / `output_schema` |

**Exception kinds.** BindError is illegal YAML/request (helper vs grain, listed name clash, `last_n` as numeric `of`, missing SQL walked column). CatalogError is data shape at apply (helper group `n>1`, unlisted name clash, tz-aware dates, numeric-as-date). FilterError is a valued leftover filter that is not time-like and does not bind.

---

## 15. Limits

Known boundaries, so you do not design around something that is not there:

- Timestamps are bucketed as stored; there is no timezone conversion, and `time.timezone` is rejected at bind rather than silently ignored. Convert the column in a `kind: sql` model if you need it.
- `calendar: fiscal` changes `quarter` and `year` only. Fiscal *months* are ordinary calendar months. There is no fiscal-week grain (DAX has none either). When the host sends a **year part**, year grain / `ytd` / `full_year` / year-part spans are calendar January–December (`TimeSpec.year_basis=calendar`) without moving fiscal quarters.
- Regex, JSON, geospatial, ML, timezone conversion, result caching, hierarchy expansion, and cross-KPI measure references remain architecture boundaries: hooks, `kind: sql` models, or the host.
- `trailing` / `offset` calendar keys (`days`, `weeks`, `months`, `quarters`, `years`) do not change meaning when `parameters.time_grain` changes. `periods` and `from: data_points` follow the pick (§5.2).
- `over:` detail is capped at 500,000 rows and 50,000 partitions; densify trends still use the 50,000 cell cap. Larger extracts fail fast — narrow filters, coarsen retrieve, or pre-aggregate in a SQL model.
- `measures.*.cuts` restricts **trend**, **rank**, **percent_of_total**, and other cut-phase kinds (`ntile`, `top_n`, …).
- `percent_of_total` defaults to **this cut's** rows (after having). Cross-cut share is `versus_cut:` naming another declared cut.
- `filters:` is the pipeline mask (then cuts / `ignore_filters`). A single measure may add `where:` / `ignore_filters:` when `of:` is a base. KPI `having:` is the measure-predicate drop.
- Rank a lagged measure (`rank of lag`); do not `lag { of: rank }`. Put `offset:` on a trend rather than `lag { of: trend }`.
- Physical joins support `inner`, `left` and `right`. Anything else belongs in a `kind: sql` model.
- KPI `base_measures.sql` is a column name or a Pandas formula. DuckDB `SUM(` / `LAG(` belong in a `kind: sql` **model** when you opt into SQL extract shaping — not in KPI YAML.
- Non-additive aggregations (`median` / `percentile` / `count_distinct` / `stddev` / `variance` / `mode`) fold the **post-pipeline** fact series, not the pre-CASE columns.
- `expr:` CASE is Pandas, not DuckDB. No `SUM(CASE)`, `LIKE`, `IN ('A','B')`, or simple `CASE status WHEN 'O'`. `columns:` + `op:` stays numeric.
- Host ownership (ADLS, auth, jobs, context builder) stays outside `compute(context)`.
- We do not claim identical IEEE bits across pandas versions; post-extract stable sort + `_kpi_row_id` makes window order deterministic.
