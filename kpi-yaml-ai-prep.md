# KPI YAML — AI prep (bind-ready files)

Give **this file** to an AI with the calculation spec. Emit complete YAML the engine can bind. Do not invent names.

| Need | Read |
|---|---|
| Key-by-key detail | [kpi-yaml-reference.md](kpi-yaml-reference.md) |
| Live op/fn/hook names | [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) (**wins** if lists disagree) |
| Human deep-dive | [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md) |
| Gold KPI shape | [kpi_config/kpis/sotif/3004.yaml](kpi_config/kpis/sotif/3004.yaml) |
| Gold model shape | [kpi_config/models/sotif/sotif.yaml](kpi_config/models/sotif/sotif.yaml) |

You write **configuration only**. You do **not** write Python, DuckDB measure SQL in KPI files, host JSON, or `kpi_engine/<id>.py`.

---

## 0. Workflow (follow in order)

1. **Intake** — Confirm §2 fields. If anything required is missing, ask numbered questions. **Do not emit YAML.**
2. **Model** — Reuse an existing `model_id` when possible. Create `models/<group>/<model_id>.yaml` only for a **new** extract (§6).
3. **KPI skeleton** — `kpi_id`, `model`, `time` or snapshot, `dimensions`, `default_dimensions`, `cuts`, `default_cut`, `row_set`, empty `measures:`.
4. **Base facts** — One `base_measures` entry per physical column the engine must retrieve and fold. Identifiers = **physical column names** (or `sql:` alias).
5. **Measures** — One key per host `measure_key`. Walk §4 decision tree. Use only catalog names from §8.
6. **Self-check** — Every row in §11 must be true. Re-read §5 anti-patterns.

**High confidence** = every checklist row is true. Partial files, `# TODO`, and guessed columns are failures.

---

## 1. Output

Every generating response, in this order:

1. Entire `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` (flat `kpis/<kpi_id>.yaml` is legal).
2. Entire `kpi_config/models/<kpi_group>/<model_id>.yaml` **only if** the extract is new. If reused: `Reuses existing model <model_id>. No new model file.`
3. **Assumptions** (inferred facts only).
4. **Gaps** — if a required field is missing, **do not emit YAML**. Ask numbered questions.

**Never:** partial files, `...`, `# TODO`, `CHANGE_ME`; invented `op`/`fn`/`hook`/`agg`/`range`/grain; ADLS/`abfss://`/`/mnt/` in KPI YAML; guessed physical columns, filter codes, or `measure_key` spellings.

If any checklist row in §11 is false, stop and ask.

---

## 2. Intake (must have these or ask)

| Required | Notes |
|---|---|
| `kpi_id`, `kpi_group` (folder only) | Filename stem = `kpi_id:` = `execution.kpi_id` **exactly** |
| `model_id` + reuse yes/no | KPI `model:` must equal model `model_id:` after fold |
| `time` **or** `snapshot: true` | Snapshot = no period column; no window/trend/nonzero offset |
| `dimensions` + `default_dimensions` | `default_dimensions` required (`[]` = worldwide) |
| ≥1 cut | Each cut needs `name`; `default_cut` must name one of them |
| Every host `measure_key` | English/math formula for each |
| Physical columns | Columns DuckDB must retrieve from the model |

Optional: `periods` map, `filters_to_declare`, dataset aliases, `kind: physical|sql` when creating a model.

---

## 3. Two files, two layers

| File | Job | Never put here |
|---|---|---|
| `models/<group>/<model_id>.yaml` | DuckDB extract (tables/joins or SQL CTE) | KPI `agg:` / `op:` / measure `expr:` |
| `kpis/<group>/<kpi_id>.yaml` | Time, dims, facts, cuts, requestable measures | Dataset URIs, Python, invented ops |

**Layer rule (most common AI mistake):**

```
WRONG — ratio in base_measures:
  fill_rate: { expr: shipped / ordered, agg: avg }   # sum of per-row ratios

RIGHT — fold each side, then combine in measures:
  base_measures:
    shipped_qty: { sql: shipped, agg: sum }
    ordered_qty: { sql: ordered, agg: sum }
  measures:
    fill_rate: { op: expr, expr: shipped_now / ordered_now }
    shipped_now: { of: shipped_qty, op: point }
    ordered_now: { of: ordered_qty, op: point }
```

- **Row math then fold** → `base_measures` (identifiers = **physical columns**).
- **After fold** → `measures` (identifiers = **measure keys** or `of:` a base).
- One of `sql:` / `columns:`+`op:` / `expr:` / `lookup:` / `over:` per base — **never mix** on the same base.

Host `selected_dimensions` picks from YAML `dimensions:` (GROUP BY, not math). Do **not** declare `parameters.selected_dimensions`. Empty `measures_required: []` computes nothing.

Identifiers: `^[A-Za-z_][A-Za-z0-9_]*$`. Formula reserved words: `case when then else and or not is null in`.

---

## 4. Map spec → YAML (first match wins)

| User wants… | YAML |
|---|---|
| SUM/COUNT/MIN/MAX/AVG/distinct of a column at the selected period | `base_measures.x: { sql: col, agg: … }` + `measures.k: { of: x, op: point }` |
| Same metric **previous calendar** year/month/quarter/week/day | `op: point` + `offset: { years\|months\|quarters\|weeks\|days: 1 }` |
| Same metric **previous period at current grain** (e.g. prior quarter when grain=quarter) | `op: point` + `offset: { periods: 1 }` |
| Period compare (YoY / MoM / WoW / QoQ / prior bucket / gap) on a **named base** | `op: compare` + `of:` the base + `mode:` (table below). One host key. |
| Conditional SUM/COUNT from a **physical column** (no extra named base) | `op: filtered_point` (or `filtered_window` / `filtered_trend`) + `column:` + `where:` |
| Conditional fact **and** period compare in one key | `op: filtered_compare` + `column:` or `of:` + `where:` + `mode:` |
| Host must also request current **and** previous as their own keys | two `point` measures + `op: fn`/`arithmetic` `fn: growth_pct` (gold 3004). Do not rewrite 3004. |
| Trailing N as **one number** | `op: window` + `trailing: { months: N }` + `inclusive: true` |
| YTD/QTD/MTD/WTD | `op: window` + `range: ytd` (etc.). `qtd` ≠ trailing 3. `wtd` needs grain `day` |
| Graph / sparkline array | `op: trend` + `trailing:`. Default cut only unless `cuts:` |
| Graph of a ratio of two **period totals** | `op: trend_arithmetic` + `expr:` over two `sum` bases. **Not** `trend of` a composite |
| Ratio of two **already aggregated** measures | `op: expr` or `op: fn` (`divide`, `percent`, …) |
| This group as % of all groups on this cut | `op: percent_of_total` (**not** `fn: percent`) |
| Share of **another cut's** total | `percent_of_total` + `versus_cut:` |
| Rank groups | `op: rank` + `order: desc\|asc`. Rank a lagged measure; never `lag { of: rank }` |
| Last period of a composite (ratio/OEE/window/hook) | `op: compare` / `lag` / `diff` / `pct_change` of that measure. Not of `trend`/`rank`/row helper |
| Trend prior year | `offset:` **on the trend measure**, not a separate `lag { of: trend }` |
| Per-row product then SUM | helper `expr:` base, then another base with `agg: sum` |
| Mask rows / ignore one context filter on **one** measure | `where:` / `ignore_filters:` on the measure; `of:` must be a **base**. Or `filtered_*` + `column:` / `of:` |
| Hit-rate / EWMA / CAGR / MAD / forecast | `op: hook` + catalog name + `trailing:` |
| Keep rows vs drop groups | `op: predicate` (1/0) vs top-level `having:` |
| Fixed target / goal | `op: constant` + `value:` (scalar or map + `by:` + `default:`) |
| Echo a dimension as a requestable column | `kind: dimension` (key must match a `dimensions:` name) |
| Entity lag / running sum on order rows | `over:` on a pre-fold base (not calendar `op: lag`) |
| Code→fee map | `lookup:` on a base |
| No period column | omit `time:`; only `point` / `filtered_point` offset 0 and `constant` |
| Math no catalog row covers | **Stop.** Name the missing catalog entry. Do not fake with `expr:` |

**Period compare (canonical — one host key):**

```yaml
measures:
  yoy: { op: compare, of: fact, mode: yoy }
```

Gold 3004 still uses two `point` keys + `growth_pct` because the host requests current and previous as their own `measure_key`s. Do not rewrite 3004. Use that shape only when the page needs those extra keys.

| `mode` | Expands to | Grain guard |
|---|---|---|
| `yoy` | `pct_change` + `offset: { years: 1 }` | none |
| `mom` | `pct_change` + `offset: { months: 1 }` | `time.grain` must be `month` |
| `wow` | `pct_change` + `offset: { weeks: 1 }` | `time.grain` must be `week` |
| `qoq` | `pct_change` + `offset: { periods: 1 }` | `time.grain` must be `quarter` |
| `pop` | `pct_change` + `offset: { periods: 1 }` | none — prior bucket at **effective** grain |
| `diff` / `pct_change` | `diff` / `pct_change` | `versus:` required (same keys as `offset:`) |

`mode: pop` when the KPI declares `parameters.time_grain` (named `mom`/`wow`/`qoq` bind against YAML `time.grain` only). Do not combine a preset with `versus:` (`use mode: pct_change` or `mode: diff` with `versus:`). `compare` does not take `column:` — that is `filtered_*` only.

YoY of a **window** is that window vs the same window one year earlier (not a window of YoY): `{ op: compare, of: value_3m, mode: yoy }`.

**Filtered fact (canonical):**

```yaml
measures:
  closed_amount:
    op: filtered_point
    column: amount
    agg: sum                    # default sum; omit agg: when of: names a base
    where: { column: status, op: eq, value: closed }
  closed_yoy:
    op: filtered_compare
    column: amount
    where: { column: status, op: eq, value: closed }
    mode: yoy
```

`column:` xor `of:` (a **base** with `agg:`). `where:` required; `WHERE_OPS` only (no `like`). `agg:` only with `column:`. Snapshot: `filtered_point` only. Keys starting `__` are reserved.

---

## 5. Anti-patterns (WRONG → RIGHT)

These patterns cause the most bind errors. Fix before emitting.

| WRONG | RIGHT | Bind / logic error |
|---|---|---|
| `offset: { year: 1 }` | `years: 1` | `unknown key` on offset |
| `trailing: { month: 3 }` | `months: 3` | `unknown key` on trailing |
| `offset: { periods: 1, months: 1 }` | one of them only | `cannot mix` periods with calendar units |
| `fiscal_start_month: 4` without `calendar: fiscal` | add `calendar: fiscal` | `fiscal_start_month requires time.calendar: fiscal` |
| `fn: percent` for group share | `op: percent_of_total` | percent is row-level fn, not cut share |
| `op: trend` on a ratio composite | `op: trend_arithmetic` | trend wraps one base series |
| `agg: avg` on `shipped/ordered` per row | two `sum` bases + measure `expr` | wrong business math |
| `group_by: [reason_code, region]` when both are request dims | `group_by: []` or extras only | effective grain already includes request dims |
| `ignore_filters: [region]` alone | also `exclude_from_grain: [region]` | dim-named ignore must pair both ways |
| `lag: { of: trend_12m }` | `offset: { years: 1 }` on the trend | cannot lag cut-phase ops |
| `partition_by:` on calendar `op: lag` | `over.partition_by` on a base | calendar lag has no partition_by |
| DuckDB `SUM(...)` in KPI yaml | `base_measures` + `sql:` + `agg:` | layer mix |
| measure `expr:` referencing physical columns | base `expr:` then `agg:`, or point measures | wrong namespace |
| `where:` on measure with `of: current_value` | `where:` only when `of:` is a **base** | bind error |
| `parameters.selected_dimensions` | host sends `selected_dimensions` top-level | bind error |
| `time` filters in `filters:` block | `time.filter_code` or `time.periods:` | year/month belong on time |
| `periods:` and `compose:` together | pick one | mutually exclusive |
| Invented `op: rolling_mean` | catalog hook or `op: fn` | unknown op |
| UI helper in `measures:` only | helpers in `base_measures:`; only host keys in `measures:` | orphan or missing keys |
| `op: compare` + `column:` | `filtered_compare` + `column:` (or `compare` + `of:` a named base) | `column` is filtered-only |
| `mode: yoy` + `versus:` | `mode:` preset **or** `mode: pct_change`/`diff` + `versus:` | `versus` + `pct_change`/`diff` |
| `mode: qoq` at `time.grain: month` | `time.grain: quarter`, or `mode: pop` | `qoq` + `quarter` + `pop` |
| `mode: mom` at non-month grain | `time.grain: month`, or `mode: pop` | `mom` + `month` |
| `filtered_*` with both `column:` and `of:` | pick one | `column` + `of` |
| `filtered_*` without `where:` | add `where:` | `where` |
| `filtered_* of:` a measure | `of:` a **base** (or `column:`) | `requires of: a base` |
| `agg:` on `filtered_* of:` a base | `agg:` only with `column:` | `agg` |
| Authored `measures.__x` / `base_measures.__x` | names must not start with `__` | `reserved` |
| Snapshot + `compare` / `filtered_window` / `filtered_trend` / `filtered_compare` | snapshot allows `filtered_point` only | `needs a time` / `no time: block` |

---

## 6. Offset & trailing (calendar vs grain)

**Allowed keys** (only these — singular forms fail bind):

| Block | Keys |
|---|---|
| `offset:` | `days`, `weeks`, `months`, `quarters`, `years`, `periods` |
| `trailing:` | same set + `from: data_points` |

**Rules:**

- **Calendar units** (`years`, `months`, …) keep calendar meaning after a `time_grain` pick.
- **`periods: N`** = N steps at the **effective grain** (prior quarter when grain=quarter, prior month when grain=month).
- **Never mix** `periods` with any calendar unit in the same block.
- Snapshot KPIs: only `offset: { months: 0 }` on `point` / `filtered_point`; no window/trend/compare/`filtered_window`/`filtered_trend`/`filtered_compare`/hook/period hooks.

| `compare` `mode` | Offset | Required `time.grain` |
|---|---|---|
| `yoy` | `{ years: 1 }` | any |
| `mom` | `{ months: 1 }` | `month` |
| `wow` | `{ weeks: 1 }` | `week` |
| `qoq` | `{ periods: 1 }` | `quarter` |
| `pop` | `{ periods: 1 }` | any (prior bucket at effective grain) |
| `diff` / `pct_change` | `versus:` | any |

Named `mom`/`wow`/`qoq` bind against YAML `time.grain` only. If the KPI declares `parameters.time_grain`, use `mode: pop`.

```yaml
# Previous month (calendar) at month grain
offset: { months: 1 }

# Previous quarter at quarter grain (one bucket back)
offset: { periods: 1 }

# Trailing 3 months ending at anchor
trailing: { months: 3 }
inclusive: true
```

---

## 7. Model YAML (often skipped — do not skip)

Create only when the extract is new. Reuse beats inventing a second model.

**Physical model** (one table / parquet alias):

```yaml
model_id: <model_id>
kind: physical
required_aliases: [<alias>]    # must exist on context.datasets
sources:
  <alias>: { alias: <alias> }
joins: []
```

**SQL model** — also needs `sql:` + `output_schema:` (walked columns). Prefer `$alias_scan`. Context IN + time range wrap the **final SELECT**.

| Rule | Detail |
|---|---|
| `model_id:` | KPI `model:` must match after fold (`sotif` ≠ `sotif_sql`) |
| `kind:` | `physical` or `sql` only |
| Physical | needs `required_aliases` or `sources` |
| SQL | needs `sql:` block |
| Joins | `inner` / `left` / `right` only |
| Never | KPI `agg:`, measure math, dataset paths in KPI yaml |

Gold: [kpi_config/models/sotif/sotif.yaml](kpi_config/models/sotif/sotif.yaml).

---

## 8. Closed names (do not invent)

If a name is not listed here or in CAPABILITIES.md, **stop and ask** — do not emit fake YAML.

**`agg:`** `sum` `avg` `count` `count_distinct` `min` `max` `median` `percentile` (+ `percentile:`) `first` `last` `stddev` `variance` `mode`.

**Measure `op:` / `kind:`** `point` `window` `trend` `trend_arithmetic` `arithmetic` `fn` `expr` `constant` `dimension` `predicate` `hook` `rank` `percent_of_total` `ntile` `dense_rank` `row_number` `cumulative_share` `running_total` `contribution` `lag` `lead` `index` `vs_target` `threshold` `percent_rank` `gap_to_leader` `gap_to_avg` `zscore` `running_avg` `top_n` `diff` `pct_change` `compare` `filtered_point` `filtered_window` `filtered_trend` `filtered_compare`.

Prefer `op: fn` + `inputs:` when operand order must not swap. `arithmetic` = `left`/`right` or `of: [a,b]`. Measure `expr` = nested `+ - * /` and CASE over **measure keys** only.

**Measure fns:** `growth_pct` `divide` `percent` `sum` `subtract` `multiply` `min` `max` `avg` `abs` `clamp` `attainment` `coalesce` `if_null` `nullif` `null_if_zero` `zero_if_null` `is_null` `is_not_null` `if_else` `sign_label` `round` `floor` `ceil` `power` `log` `log10` `sqrt` `date_diff` `date_add` `epoch_day`.

**Column ops (`columns:`+`op:` on bases):** `value` `abs` `sum` `subtract` `multiply` `divide` `percent_of` `min` `max` `avg` `coalesce` `if_null` `nullif` `null_if_zero` `zero_if_null` `is_null` `is_not_null` `if_else` `round` `floor` `ceil` `power` `log` `log10` `sqrt` `date_diff` `date_add` `epoch_day`.

**Hooks:** `seasonal_index` `ewma` `period_max` `period_min` `period_median` `period_avg` `period_sum` `hit_rate` `streak` `period_stdev` `period_var` `period_cv` `period_range` `period_count` `miss_rate` `miss_streak` `longest_streak` `cagr` `slope` `mad` `projection`. Always set `trailing:`/`offset:`.

**Grains:** `day` `week` `month` `quarter` `year`. Week = ISO Monday.

**Window `range:`:** `trailing` `leading` `cumulative` `ytd` `mtd` `qtd` `wtd` `full_month` `full_quarter` `full_year`. Named PTD cannot also set `trailing:`.

**`where:` ops:** `in` `eq` `ne` `gt` `gte` `lt` `lte` `between` (`values: [lo,hi]`). Not `like`/`is_null`. `ne` excludes nulls.

Host names fold (case/space/underscore; measure keys also compact-fold). Do not create colliding keys.

---

## 9. KPI skeleton

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
  # fiscal_start_month: 4     # only with calendar: fiscal

dimensions:
  - { name: <dim>, from: <physical_column> }

default_dimensions: [<dim>]   # required; [] = worldwide

base_measures:
  <fact>: { sql: <physical_column>, agg: sum }

cuts:
  - name: G
    group_by: []              # extras ONLY — not names in default_dimensions
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

Copy structure from [3004.yaml](kpi_config/kpis/sotif/3004.yaml); replace ids, columns, and measure keys — not Sotif names unless this KPI is Sotif.

---

## 10. Rules that fail bind if ignored

**Time.** Need `column` + `grain` plus one of `filter_code` / `periods:` / `compose:` (not `periods` and `compose` together). Scalar `filter_code` on the context = exactly one value and **wins**. `periods:` parts conjoin; missing part = not applied; lists = union. Month part accepts `3`, `"03"`, `March`, `Mar`. Never `WHERE month IN (one month)` for lookback — the engine scans a date range. Finer pick than `source_grain` fails. `time.timezone` rejected. Fiscal = quarter/year only. Snapshot: no window/trend/nonzero offset/period hooks; `filtered_point` is allowed, `compare` / other `filtered_*` are not.

**Cuts.** `group_by` = **extras only**. Effective grain = request dims − `exclude_from_grain` + extras. Dim-named `ignore_filters` must pair with `exclude_from_grain`. `measures.*.cuts` only limits trend/rank/`percent_of_total` (cut-phase ops). Trend default = `default_cut` (50k cells/cut).

**Filters.** Year/month belong in `time:`, not `filters:`. Undeclared context IN is extract unless a cut ignores the code. `apply: extract` (cheap) / `calc` / `result`. Unmapped **valued** filter = error. Empty/`[]`/all-null = skip. No `optional: false`.

**Helpers.** `expr`/`lookup`/`over` without `agg:` are row helpers: `measures.of` only at `identity_grain`. Calendar `op: lag` cannot set `partition_by`. `over.partition_by` ⊆ cut grain.

**Multi-model.** `base_measures.*.model:` + `model_relations: { left, right, on, how }`. Join after aggregate.

**Parameters.** Sibling of `filters`, not `execution.*`. Reserved: `time_grain`, `output_cut`. No `parameters:` block → reject a non-empty `context.parameters`. No `execution.time_grain`.

**Constants by region:**

```yaml
measures:
  target:
    op: constant
    by: region
    default: 100
    value: { EMEA: 120, NA: 110 }
```

**Timed output shapes.** Point/lag → `{value, period}`. Window/ytd → `{value, period_start, period_end}`. Trend → `[{period, value}, …]`. Rank, percent, constants, yoy/fn/expr composites → scalars (with `period` on request bucket where applicable). `trend_axes` stays on the envelope.

**Closed platform gaps (do not invent YAML):** timezone conversion, cross-KPI measure refs, hierarchy expansion, regex/JSON/geo/ML, result caching. Use a catalog hook, `kind: sql`, or the host.

If the spec needs a **new** catalog name: say cannot bind, name the gap, do not emit fake YAML.

---

## 11. Emit checklist (all must be true)

- [ ] Filename stem = `kpi_id:` = `execution.kpi_id` (exact); `.yaml`; id unique.
- [ ] `model:` matches a real `model_id:` (fold).
- [ ] `default_dimensions` present (`[]` ok); ≥1 cut; `default_cut` is declared; `group_by` extras only.
- [ ] Every host `measure_key` is under `measures:`. UI-unrequested helpers stay in `base_measures:` only.
- [ ] Every `of:` / `left`/`right` / `inputs:` / measure `expr:` name is a declared base or measure (base `expr:` may name physical columns).
- [ ] Identifiers match `^[A-Za-z_][A-Za-z0-9_]*$`.
- [ ] Time vs snapshot rules in §10. `row_set` is `span_union` or `anchor_only`.
- [ ] Offset/trailing keys are plural (`years` not `year`; `months` not `month`).
- [ ] No dataset URI, no invented names, no `parameters.selected_dimensions`.
- [ ] Ratio KPIs use two folded bases + measure-level combine (§3).
- [ ] Dimension echo uses `kind: dimension`; share uses `percent_of_total`.

---

## 12. Complete mini example (bind-ready)

```yaml
kpi_id: 3004
version: 1
model: sotif

time:
  column: event_month
  grain: month
  filter_code: reporting_month
  calendar: gregorian

dimensions:
  - { name: reason_code, from: reason_code }
  - { name: region, from: region }

default_dimensions: [reason_code]

base_measures:
  sotif_value: { sql: amount, agg: sum }

cuts:
  - { name: G, group_by: [], exclude_from_grain: [region], ignore_filters: [region], also_emit: [R] }
  - { name: R, group_by: [region], exclude_from_grain: [], ignore_filters: [] }

default_cut: G
row_set: span_union

measures:
  reason_code:       { kind: dimension }
  current_value:     { of: sotif_value, op: point, offset: { months: 0 } }
  previous_year_value: { of: sotif_value, op: point, offset: { years: 1 } }
  value_3m:          { of: sotif_value, op: window, trailing: { months: 3 }, inclusive: true }
  yoy_month:         { op: arithmetic, fn: growth_pct, left: current_value, right: previous_year_value }
  trend_12m:         { of: sotif_value, op: trend, trailing: { months: 12 }, inclusive: true, cuts: [G] }
```
