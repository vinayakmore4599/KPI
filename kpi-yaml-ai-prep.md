# KPI YAML — AI prep (bind-ready files)

Give **this file** to an AI with the calculation spec. Emit complete YAML the engine can bind. Do not invent names. For key-by-key detail use [kpi-yaml-reference.md](kpi-yaml-reference.md); for live names use [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) (wins if lists disagree).

You write configuration. You do **not** write Python, DuckDB measure SQL, host JSON, or `kpi_engine/<id>.py`.

---

## 1. Output

Every generating response, in this order:

1. Entire `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` (flat `kpis/<kpi_id>.yaml` is legal).
2. Entire `kpi_config/models/<kpi_group>/<model_id>.yaml` **only if** the extract is new. If reused: `Reuses existing model <model_id>. No new model file.`
3. **Assumptions** (inferred facts only).
4. **Gaps** — if a required field is missing, **do not emit YAML**. Ask numbered questions.

**Never:** partial files, `...`, `# TODO`, `CHANGE_ME`; invented `op`/`fn`/`hook`/`agg`/`range`/grain; ADLS/`abfss://`/`/mnt/` in KPI YAML; guessed physical columns, filter codes, or `measure_key` spellings.

If any checklist row in §8 is false, stop and ask.

---

## 2. Intake (must have these or ask)

`kpi_id`, `kpi_group` (folder only), `model_id` + reuse yes/no, `time` **or** `snapshot: true`, `dimensions` + `default_dimensions`, ≥1 cut, every host `measure_key` with an English/math formula, physical columns DuckDB must retrieve.

Optional: `periods` map, `filters_to_declare`, dataset aliases, `kind: physical|sql` when creating a model.

---

## 3. Two files, two layers

| File | Job | Never |
|---|---|---|
| `models/<group>/<model_id>.yaml` | DuckDB extract (tables/joins or SQL CTE) | KPI `agg:` / `op:` / measure `expr:` |
| `kpis/<group>/<kpi_id>.yaml` | Time, dims, facts, cuts, requestable measures | Dataset URIs, Python, invented ops |

KPI `model:` **must equal** model `model_id:` after fold (case/space/underscore). `sotif` ≠ `sotif_sql`. KPI filename stem = `kpi_id:` = `execution.kpi_id` **exactly**. One optional group folder. Ids unique across groups. Identifiers: `^[A-Za-z_][A-Za-z0-9_]*$`. Formula reserved: `case when then else and or not is null in`.

**Do not mix layers.** Row math then fold = `base_measures` (identifiers = **physical columns**). After fold = `measures` (identifiers = **measure keys** or `of:` a base). Fill rate is almost always ratio of two totals, not `SUM(shipped/ordered)`. One of `sql:` / `columns:`+`op:` / `expr:` / `lookup:` / `over:` per base — never mix.

Host `selected_dimensions` picks from YAML `dimensions:` (GROUP BY, not math). Do not declare `parameters.selected_dimensions`. Empty `measures_required: []` computes nothing.

---

## 4. Map spec → YAML (first match wins)

| Spec | YAML |
|---|---|
| SUM/COUNT/MIN/MAX/AVG/distinct of a column at the selected period | `base_measures.x: { sql: col, agg: … }` + `measures.k: { of: x, op: point }` |
| Same metric last year/month/quarter/week | `op: point` + `offset: { years\|months\|quarters\|weeks: 1 }` |
| YoY / growth % | two points + `op: fn`, `fn: growth_pct`, `inputs: { current, previous }` (`0.05` = +5%; use `percent` for 0–100) |
| Trailing N as **one number** | `op: window` + `trailing: { months: N }` + `inclusive: true` |
| YTD/QTD/MTD/WTD | `op: window` + `range: ytd` (etc.). `qtd` ≠ trailing 3. `wtd` needs grain `day` |
| Graph / sparkline array | `op: trend` + `trailing:`. Default cut only unless `cuts:` |
| Ratio of two **already aggregated** measures | `op: expr` or `op: fn` (`divide`, `percent`, …) |
| This group as % of all groups on this cut | `op: percent_of_total` (not `fn: percent`) |
| Share of **another cut’s** total | `percent_of_total` + `versus_cut:` |
| Rank groups | `op: rank` + `order: desc\|asc`. Rank a lagged measure; never `lag { of: rank }` |
| Last period of a composite (ratio/OEE/window/hook) | `op: lag`/`diff`/`index`/`pct_change` of that measure. Not of `trend`/`rank`/row helper. Trend prior year: `offset:` **on the trend** |
| Per-row product then SUM | helper `expr:` then another base with `agg: sum` |
| Mask rows / ignore one context filter on **one** measure | `where:` / `ignore_filters:` on the measure; `of:` must be a **base** |
| Hit-rate / EWMA / CAGR / MAD / forecast | `op: hook` + catalog name + `trailing:` |
| Keep rows vs drop groups | `op: predicate` (1/0) vs top-level `having:` |
| Target / dim echo | `op: constant` + `value:` / `op: dimension` |
| Entity lag / running sum on order rows | `over:` on a pre-fold base (not calendar `op: lag`) |
| Code→fee map | `lookup:` on a base |
| No period column | omit `time:`; only `point` offset 0 and `constant` |
| No row matches | **Stop.** Name the missing catalog entry. Do not fake with `expr:` |

---

## 5. Closed names

**`agg:`** `sum` `avg` `count` `count_distinct` `min` `max` `median` `percentile` (+ `percentile:`) `first` `last` `stddev` `variance` `mode`. Non-additive re-read rows. `stddev`/`variance` sample ddof=1 (1 row → null). `mode` ties → smallest. `count` on text = row count.

**`op:` (measures)** `point` `window` `trend` `arithmetic` `fn` `expr` `constant` `dimension` `predicate` `hook` `rank` `percent_of_total` `ntile` `dense_rank` `row_number` `cumulative_share` `running_total` `contribution` `lag` `lead` `index` `vs_target` `threshold` `percent_rank` `gap_to_leader` `gap_to_avg` `zscore` `running_avg` `top_n` `diff` `pct_change`.

Prefer `op: fn` + `inputs:` when order must not swap. `arithmetic` = `left`/`right` or `of: [a,b]`. `expr` = nested `+ - * /` and CASE over **measure keys**. Rank/`percent_of_total` may feed `arithmetic`/`fn`/`expr`; trends/hooks may not consume cut ops.

**Measure fns:** `growth_pct` `divide` `percent` `sum` `subtract` `multiply` `min` `max` `avg` `abs` `clamp` `attainment` `coalesce` `if_null` `nullif` `null_if_zero` `zero_if_null` `is_null` `is_not_null` `if_else` `sign_label` `round` `floor` `ceil` `power` `log` `log10` `sqrt` `date_diff` `date_add` `epoch_day`. Sum/sub/mul/div: any null → null. Divide-by-zero → null. `sign_label` is a string leaf.

**Column ops (`columns:`+`op:`):** `value` `abs` `sum` `subtract` `multiply` `divide` `percent_of` `min` `max` `avg` `coalesce` `if_null` `nullif` `null_if_zero` `zero_if_null` `is_null` `is_not_null` `if_else` `round` `floor` `ceil` `power` `log` `log10` `sqrt` `date_diff` `date_add` `epoch_day`. List = positional; map = named (`divide`/`percent_of`).

**Hooks:** `seasonal_index` `ewma` `period_max` `period_min` `period_median` (`rolling_median`) `period_avg` `period_sum` `hit_rate` `streak` `period_stdev` `period_var` `period_cv` `period_range` `period_count` `miss_rate` `miss_streak` `longest_streak` `cagr` `slope` `mad` `projection`. Always set `trailing:`/`offset:`.

**Grains:** `day` `week` `month` `quarter` `year`. Week = ISO Monday. **Window `range:`:** `trailing` `leading` `cumulative` `ytd` `mtd` `qtd` `wtd` `full_month` `full_quarter` `full_year`. Named PTD cannot also set `trailing:`.

**`where:` ops (base or measure):** `in` `eq` `ne` `gt` `gte` `lt` `lte` `between` (`values: [lo,hi]`). Not `like`/`is_null`. `ne` excludes nulls.

Host names fold (case/space/underscore; measure keys also compact-fold). Do not create colliding keys.

---

## 6. Skeleton

Drop commented blocks only when intake says they do not apply. Do not drop `kpi_id`, `model`, `default_dimensions`, `cuts`, `measures`. Update header comments for **this** id (not leftover 3004/Sotif).

```yaml
kpi_id: <kpi_id>
version: 1
model: <model_id>

time:                         # omit whole block if snapshot
  column: <time_column>
  grain: month                # day | week | month | quarter | year
  filter_code: <host_filter>  # or periods: { year: year, month: "current month" }
  calendar: gregorian
  # anchor: last_observed     # opt-in; default selection_end
  # max_span_years: 10        # opt-in hard cap

dimensions:
  - { name: <dim>, from: <physical_column> }

default_dimensions: [<dim>]   # required; [] = worldwide

base_measures:
  <fact>: { sql: <physical_column>, agg: sum }

cuts:
  - name: G
    group_by: []              # extras only — not names in default_dimensions
    exclude_from_grain: []
    ignore_filters: []
    also_emit: []

default_cut: G
row_set: span_union           # or anchor_only

measures:
  <measure_key>:
    of: <fact>
    op: point
    offset: { months: 0 }
```

New physical model (only if not reusing):

```yaml
model_id: <model_id>
kind: physical
required_aliases: [<alias>]
sources:
  <alias>: { alias: <alias> }
joins: []
```

`kind: sql` also needs `sql:` + `output_schema:` (walked columns). Prefer `$alias_scan`. Context IN + time range wrap the **final SELECT**. Physical joins: `inner`/`left`/`right` only.

Gold shape: `kpi_config/kpis/sotif/3004.yaml` — copy structure, not Sotif names, unless this KPI is Sotif.

---

## 7. Rules that fail bind if ignored

**Time.** Need `column` + `grain` plus one of `filter_code` / `periods:` / `compose:` (not `periods` and `compose` together). Scalar `filter_code` on the context = exactly one value and **wins**. `periods:` parts conjoin; missing part = not applied; lists = union. Month part accepts `3`, `"03"`, `March`, `Mar`. Never `WHERE month IN (one month)` for lookback — the engine scans a date range. `offset`/`trailing` calendar units (`days` `weeks` `months` `quarters` `years`) keep that meaning after a grain pick; `periods` / `from: data_points` follow the pick. Finer pick than `source_grain` fails. `time.timezone` rejected. Fiscal = quarter/year only (no fiscal weeks). Snapshot: no window/trend/nonzero offset/period hooks.

**Cuts.** `group_by` = extras only. Effective grain = request dims − `exclude_from_grain` + extras. Dim-named `ignore_filters` must pair with `exclude_from_grain`. `measures.*.cuts` only limits trend/rank/`percent_of_total` (and other cut-phase ops). Trend default = `default_cut` (50k cells/cut). `output_cut` on the **context** is the also_emit walk root; YAML `default` does not lock; `pack_also_emit: false` emits only that root.

**Filters.** Year/month belong in `time:`, not `filters:`. Undeclared context IN is extract unless a cut ignores the code. `apply: extract` (cheap) / `calc` / `result` (hide JSON rows; share still includes hidden). Unmapped **valued** filter = error. Empty/`[]`/all-null = skip. No `optional: false`. No `input_text: heir`. Measure `where:`/`ignore_filters:` only when `of:` is a base.

**Helpers.** `expr`/`lookup`/`over` without `agg:` are row helpers: `measures.of` only at `identity_grain` (every emitted cut must match). Calendar `op: lag` cannot set `partition_by`. `over.partition_by` ⊆ cut grain.

**Multi-model.** `base_measures.*.model:` + `model_relations: { left, right, on, how }`. Join after aggregate. Spanning two extracts without relations = BindError.

**Parameters.** Sibling of `filters`, not `execution.*`. Reserved: `time_grain`, `output_cut`. Overlay: `when: { param, cases, else }` (always `else:`) or `from_param:`. No `parameters:` block → reject a non-empty `context.parameters`. No `execution.time_grain`.

**Nulls.** Engine never emits NaN/Inf. Point empty → null. Trend `sum`/`count` empty slot → `0`, else null. Dims not in a cut grain → `null` on the row.

**Closed (do not invent YAML for):** timezone conversion, cross-KPI measure refs, hierarchy expansion, regex/JSON/geo/ML, result caching. Use a catalog hook, `kind: sql`, or the host instead.

If the spec needs a **new** catalog name: say cannot bind, name the gap, do not emit fake YAML.

---

## 8. Emit checklist (all true)

- Filename stem = `kpi_id:` = `execution.kpi_id` (exact); `.yaml`; id unique.
- `model:` matches a real `model_id:` (fold).
- `default_dimensions` present (`[]` ok); ≥1 cut; `default_cut` is declared; `group_by` extras only.
- Every host `measure_key` is under `measures:`. UI-unrequested helpers stay in `base_measures:` only.
- Every `of:` / `left`/`right` / `inputs:` / measure `expr:` name is a declared base or measure (base `expr:` may name physical columns).
- Identifiers match `^[A-Za-z_][A-Za-z0-9_]*$`.
- Time vs snapshot rules in §7. `row_set` is `span_union` or `anchor_only`.
- No dataset URI, no invented names, no `parameters.selected_dimensions`.

---

## 9. Tiny examples

```yaml
# YoY + 3m window
measures:
  current_value:       { of: fact, op: point, offset: { months: 0 } }
  previous_year_value: { of: fact, op: point, offset: { years: 1 } }
  value_3m:            { of: fact, op: window, trailing: { months: 3 }, inclusive: true }
  yoy:                 { op: fn, fn: growth_pct, inputs: { current: current_value, previous: previous_year_value } }
```

```yaml
# Ratio of totals (right) vs sum of per-row ratios (usually wrong)
base_measures:
  shipped_qty: { sql: shipped, agg: sum }
  ordered_qty: { sql: ordered, agg: sum }
measures:
  shipped_now: { of: shipped_qty, op: point }
  ordered_now: { of: ordered_qty, op: point }
  fill_rate:   { op: expr, expr: shipped_now / ordered_now }
```

```yaml
# G worldwide + R by region (ignore_filters couples to exclude_from_grain)
default_dimensions: [reason_code]
cuts:
  - { name: G, group_by: [], exclude_from_grain: [region], ignore_filters: [region], also_emit: [R] }
  - { name: R, group_by: [region], ignore_filters: [] }
default_cut: G
```
