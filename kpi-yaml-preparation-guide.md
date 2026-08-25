# KPI YAML preparation guide

How to write `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` (and the model it points at) so the host can request measures and the engine can compute them.

**Give this file to any AI together with the calculation spec** (intake block in §0.2, plus every `measure_key` formula). The AI must return **complete, bind-ready YAML files** — not snippets, not TODOs, not a skeleton with blanks. The same contract is on the **YAML preparation** sheet of `docs/KPI-Engine-Capabilities.xlsx`.

This is the **write-it** document: what to declare, which function to pick, how columns and expressions work, and what the engine will not do. Sections after §0 are the human deep-dive.

Related docs:

- [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) — live list of every op, function, and hook
- [kpi-onboarding-guide.md](kpi-onboarding-guide.md) — process (which files to change)
- [kpi-yaml-reference.md](kpi-yaml-reference.md) — full key-by-key reference
- [README.md](README.md) — folders and request path
- [kpi-framework-plan.md](kpi-framework-plan.md) — architecture and locked decisions
- `docs/KPI-Engine-Capabilities.xlsx` — catalogs, naming conventions, YAML preparation, worked examples

---

## 0. AI authoring contract

Paste **this entire file** (or at least this §0 plus the catalogs) into the model, then paste the filled intake from §0.2.

### 0.1 Role and output

You are a KPI Engine YAML author for this repository. You emit configuration the engine can bind. You do not write Python, DuckDB measure SQL, or host JSON.

**Every response that generates YAML must contain, in this order:**

1. `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` — the **entire** file (header comments + every required block). Flat `kpis/<kpi_id>.yaml` is also legal.
2. `kpi_config/models/<kpi_group>/<model_id>.yaml` — the **entire** file **only if** the extract is new. Group folders need not match the KPI’s group. If an existing model is reused, write one sentence: `Reuses existing model <model_id>. No new model file.` Do not invent a second model.
3. **Assumptions** — only facts you had to infer (and how a reviewer can confirm them).
4. **Gaps** — anything that would fail bind. If a required field is missing, **do not emit YAML**. Ask numbered questions instead.

**Never:**

- Emit a partial file, `...`, `# TODO`, or `CHANGE_ME` for a required key.
- Invent `op:`, `fn:`, `hook:`, `agg:`, `range:`, or grain names that are not in this guide / the Excel catalogs.
- Put ADLS / `abfss://` / `/mnt/` paths in KPI YAML. Paths stay on `context.datasets` or model `default_paths`.
- Add `kpi_engine/<kpi_id>.py` or a per-KPI `module_path`. Host entry is always `kpi_engine.main`.
- Guess physical column names, filter codes, or `measure_key` spellings. Ask.

**High confidence** means every row of the completeness checklist in §0.7 is true. If any row is false, stop and ask. Do not ship a “best effort” YAML.

### 0.2 Intake the human must provide

Copy this block, fill it, and attach it with this guide. Square brackets are placeholders.

```text
kpi_id:                  # filename stem; kpi_config/kpis/<kpi_group>/<kpi_id>.yaml
kpi_group:               # authoring folder only (snake_case). Not a context field. Ids stay globally unique.
version: 1

model_id:                # existing model to reuse, or new id (lowercase snake_case)
reuse_existing_model:    # yes | no
dataset_aliases:         # context.datasets alias or key, e.g. [sotif]
kind:                    # physical | sql   (only if reuse_existing_model: no)

time:                    # omit this whole block and set snapshot: true if no period column
  column:                # date/timestamp on the extract (identifier)
  grain:                 # day | week | month | quarter | year
  filter_code:           # optional scalar period filter; wins if present on the context
  # periods:             # independent year/quarter/month/week/day → context codes
  #   year: year
  #   month: "current month"
  calendar: gregorian    # gregorian | fiscal
  # format:              # only if the column is not ISO (yyyymm, yyyymmdd, …)
snapshot: false

dimensions:              # YAML allowlist; request only picks from this
  - name:                # grain token the host sends (identifier)
    from:                # physical column on the extract (identifier)
default_dimensions:      # required; [] = worldwide default

cuts:                    # at least one; G/R are examples, not hardcoded
  - name: G
    extras: []           # extra dims beyond request grain
    exclude_from_grain: []
    ignore_filters: []
    also_emit: []
    # pack_also_emit: false  # lock one cut when this name is the output_cut walk root
default_cut: G

measure_keys:            # exact keys the host will send in measures_required
  - current_value
  - …

calculations:            # English or math for EVERY measure_key (and any helper facts)
  current_value:         # e.g. SUM(amount) at the selected month
  previous_year_value:   # e.g. same as current, offset 1 year
  yoy_pct:              # e.g. (current − previous) / previous × 100
  …

physical_columns:        # columns DuckDB must retrieve (time, dims, facts)
  - amount
  - …

filters_to_declare:      # optional; omit = IN at extract unless a cut ignore_filters the code
  # region: { column: region, apply: extract }
```

If `kpi_id`, at least one `measure_key` with a calculation, `model_id` (or reuse), and either `time:` or `snapshot: true` are missing — ask. Do not generate.

### 0.3 Two files, two jobs

| File | Job | Never put here |
|---|---|---|
| `kpi_config/models/<kpi_group>/<model_id>.yaml` | What DuckDB reads (tables/joins or a SQL CTE) | KPI math, `agg:`, `op:`, measure `expr:` |
| `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` | Time, dimensions, base facts, cuts, requestable measures | Dataset URIs, Python, free SQL, invented ops |

`KPI model:` **must equal** model file `model_id:` after folding case / spaces / underscores. `sotif` ≠ `sotif_sql`. Filename: KPI stem equals `execution.kpi_id` **exactly** (no fold). Model filename stem is `model_id` (fold allowed). One optional `<kpi_group>/` folder under `kpis/` and `models/` (not two levels). Ids are unique across groups; a KPI in `kpis/sotif/` may `model:` a file in `models/freight/`. Identifiers: `^[A-Za-z_][A-Za-z0-9_]*$`. Reserved in formulas: `case when then else and or not is null in` (`end` is allowed).

### 0.4 Two calculation layers (do not mix)

| Layer | YAML | Identifiers are | Result |
|---|---|---|---|
| Per retrieved row, then fold | `base_measures` + `sql:` / `columns:`+`op:` / `expr:` + `agg:` | **Physical columns** (and earlier helpers) | e.g. SUM of per-row ratios |
| After aggregation, on the period/cut row | `measures` + `op:` | **Other measure keys** (or `of:` a base) | e.g. ratio of two totals |

```yaml
# SUM of per-row ratios — usually WRONG for fill rate
base_measures:
  line_ratio: { expr: shipped / ordered, agg: sum }

# Ratio of totals — usually RIGHT for fill rate
base_measures:
  shipped_qty: { sql: shipped, agg: sum }
  ordered_qty: { sql: ordered, agg: sum }
measures:
  shipped_now: { of: shipped_qty, op: point }
  ordered_now: { of: ordered_qty, op: point }
  fill_rate: { op: expr, expr: shipped_now / ordered_now }
```

Do not mix `expr:` with `columns:` / `op:` / `sql:` on the same base measure.

### 0.5 Map each calculation to YAML (stop at the first match)

| The spec says… | Emit |
|---|---|
| Total / sum / count / min / max / distinct count of a column at the selected period | `base_measures.<fact>` with `sql:` + `agg:`, then `measures.<key> { of: <fact>, op: point, offset: { months: 0 } }` |
| Same metric last year / last month / last quarter | `op: point` + `offset: { years: 1 }` / `{ months: 1 }` / `{ quarters: 1 }` |
| YoY % / growth vs previous | Two points, then `op: fn` + `fn: growth_pct` + `inputs: [current, previous]` |
| Trailing N months (or periods) as **one number** | `op: window` + `trailing: { months: N }` + `inclusive: true`. Do not add N point keys by hand |
| YTD / QTD / MTD / WTD | `op: window` + `range: ytd` (etc.). `qtd` is not trailing 3. `wtd` needs `time.grain: day` |
| Graph / sparkline / last 12 months as an **array** | `op: trend` + `trailing:`. Restrict `cuts: [default]` if the payload would explode |
| Ratio / share **on the same row** (already aggregated) | `op: expr` or `op: fn` (`divide`, `percent`, `subtract`, …) |
| This group as % of **all groups on this cut** | `op: percent_of_total` (not `fn: percent`) |
| Rank groups | `op: rank` + `order: desc` or `asc` |
| Hit SLA in N of last M periods / EWMA / CAGR / streak | `op: hook` + a name from the Hooks catalog + `trailing:` |
| Flag healthy vs drop unhealthy groups | `op: predicate` (keep rows, 1/0) vs top-level `having:` (drop groups) |
| Fixed target / goal | `op: constant` + `value:` |
| Echo a dimension as a requestable column | `op: dimension` |
| qty × price **per row**, then SUM | helper `expr:` then a later base with `agg: sum` |
| Last period's ratio / OEE / other composite | `op: lag` / `diff` / `index` / `pct_change` of that measure (`fn`/`expr`/`window`/`hook` are shiftable). Not of `trend`/`rank` or a row helper. Rank a lagged measure (`rank of lag`). Offset a trend with `offset:` on the trend |
| Per-measure row mask / ignore a context filter | `where:` / `ignore_filters:` on the measure when `of:` is a **base** |
| Share of another cut's total | `op: percent_of_total` + `versus_cut:` |
| Per-customer lag / running sum **on order rows** | `over:` on a **pre-fold** base (not calendar `op: lag`) |
| Static code→fee map | `lookup:` on a base |
| No period column (point-in-time snapshot) | **Omit** `time:`. Only `point` at offset 0 and `constant`. No window/trend/nonzero offset |
| Math no row in this table covers | **Stop.** Name the missing catalog entry. Do not fake it with `expr:` or Python |

**Aggregations (`agg:`):** `sum`, `avg`, `count`, `count_distinct`, `min`, `max`, `median`, `percentile` (needs `percentile: 0–100`), `first`, `last`, `stddev`, `variance`, `mode`. `count` on a text id counts rows. `first`/`last` on text still coerce to numeric. `stddev`/`variance` are sample (ddof=1); one row → null. `mode` ties → smallest.

**Base `where:` ops:** `in`, `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `between`. `between` needs `values: [lo, hi]`. `ne` excludes nulls. Not `like` / `is_null`.

**Time grains:** `day`, `week`, `month`, `quarter`, `year` only. Default `month`. `week` is ISO Monday.

**Window `range:`:** `trailing`, `leading`, `cumulative`, `ytd`, `mtd`, `qtd`, `wtd`, `full_month`, `full_quarter`, `full_year`.

**Measure `op:` (closed list):** `point`, `window`, `trend`, `arithmetic`, `fn`, `expr`, `constant`, `dimension`, `predicate`, `hook`, `rank`, `percent_of_total`, `ntile`, `dense_rank`, `row_number`, `cumulative_share`, `running_total`, `contribution`, `lag`, `lead`, `index`, `vs_target`, `threshold`, `percent_rank`, `gap_to_leader`, `gap_to_avg`, `zscore`, `running_avg`, `top_n`, `diff`, `pct_change`.

Use `op: fn` when operand names must not be swapped (`inputs:`). Use `op: arithmetic` for `left`/`right` or `of: [a, b]`. Use `op: expr` for nested `+ - * /` and CASE over **measure keys**.

### 0.6 Required KPI YAML skeleton (fill every required key)

Emit this shape. Drop commented blocks only when the intake says they do not apply. Do not drop `kpi_id`, `model`, `default_dimensions`, `cuts`, or `measures`.

```yaml
# KPI definition for kpi_id <kpi_id>.
#
# What this file provides
#   time, dimensions, base_measures, cuts, measures for this KPI.
#   context.measures_required[].measure_key must match keys under measures:.
#
# Where it is used
#   binder.load_kpi(<kpi_id>). The engine does not hard-code this math.
#
# When to use
#   Host execution.kpi_id <kpi_id>. Do not put ADLS paths or Python here.
#
kpi_id: <kpi_id>              # must equal filename stem and execution.kpi_id
version: 1
model: <model_id>             # MUST equal models/<kpi_group>/<model_id>.yaml model_id:

time:                         # omit the whole block if snapshot: true
  column: <time_column>
  grain: month                # day | week | month | quarter | year
  filter_code: <host_filter>
  calendar: gregorian

dimensions:
  - { name: <dim>, from: <physical_column> }

default_dimensions: [<dim>]   # required; [] is worldwide

base_measures:
  <fact>:
    sql: <physical_column>    # or expr: / columns:+op: / lookup: / over:
    agg: sum

cuts:
  - name: G
    group_by: []              # extras only — not names already in default_dimensions
    exclude_from_grain: []
    ignore_filters: []
    also_emit: []

default_cut: G
row_set: span_union           # span_union | anchor_only

measures:
  <measure_key>:
    of: <fact>
    op: point
    offset: { months: 0 }
```

**New model file** (only when `reuse_existing_model: no`):

```yaml
# Physical DuckDB model for <model_id>.
model_id: <model_id>
kind: physical                # or sql
required_aliases: [<alias>]
sources:
  <alias>:
    alias: <alias>
joins: []
# kind: sql also needs sql: and output_schema: (walked columns).
# Prefer $alias_scan so Delta vs Parquet follows context.table_type.
```

Gold pattern in-repo: `kpi_config/kpis/sotif/3004.yaml` + `kpi_config/models/sotif/sotif.yaml`. Copy structure, not Sotif names, unless this KPI is Sotif.

### 0.7 Completeness checklist (all must be true before you emit)

- [ ] Filename stem = `kpi_id:` = `execution.kpi_id` (exact). Extension `.yaml`. One optional `kpis/<kpi_group>/` folder; `kpi_id` unique across groups.
- [ ] `model:` equals an existing or newly emitted `model_id:` (fold = case/space/underscore only).
- [ ] `default_dimensions` is present (`[]` legal). At least one `cuts[]`. `default_cut` is a declared cut name.
- [ ] `cuts[].group_by` lists extras only (no overlap with `default_dimensions`).
- [ ] Every host `measure_key` exists under `measures:`. Helpers the UI does not request live under `base_measures:` only.
- [ ] Every `of:`, `left`/`right`, `inputs:`, and `expr:` identifier is a declared base or measure (or a physical column on a base `expr:`).
- [ ] Every identifier matches `^[A-Za-z_][A-Za-z0-9_]*$`. No hyphens, dots, or spaces in YAML names.
- [ ] If `time:` is present: `column` and `grain` are set, plus at least one of `filter_code`, `periods:`, or `compose:`. Scalar `filter_code` is one value when present. `periods:` parts are independent predicates (lists allowed). Never `WHERE month IN (one month)`.
- [ ] If `time:` is omitted: no `window` / `trend` / nonzero `offset` / period hooks.
- [ ] No dataset URI in the KPI file. No invented `op`/`fn`/`hook`. No `parameters.selected_dimensions`. No `execution.time_grain`.
- [ ] Header comments updated for **this** kpi_id (not leftover 3004/Sotif text).
- [ ] `row_set` is `span_union` or `anchor_only`.

If a calculation needs a new catalog name (new hook, new op, new function): say **cannot bind with current catalog**, name the gap, and do not emit fake YAML.

### 0.8 Closed-world names

If the attached Excel / `CAPABILITIES.md` disagrees with a list below, **the catalog wins**.

- **Measure functions (`op: fn` / `arithmetic`):** `growth_pct`, `divide`, `percent`, `sum`, `subtract`, `multiply`, `min`, `max`, `avg`, `abs`, `clamp`, `attainment`, `coalesce`, `if_null`, `nullif`, `null_if_zero`, `zero_if_null`, `is_null`, `is_not_null`, `if_else`, `sign_label`, `round`, `floor`, `ceil`, `power`, `log`, `log10`, `sqrt`, `date_diff`, `date_add`, `epoch_day`.
- **Column functions (`base_measures` `columns:`+`op:`):** `value`, `abs`, `sum`, `subtract`, `multiply`, `divide`, `percent_of`, `min`, `max`, `avg`, `coalesce`, `if_null`, `nullif`, `null_if_zero`, `zero_if_null`, `is_null`, `is_not_null`, `if_else`, `round`, `floor`, `ceil`, `power`, `log`, `log10`, `sqrt`, `date_diff`, `date_add`, `epoch_day`.
- **Hooks (`op: hook`):** `seasonal_index`, `ewma`, `period_max`, `period_min`, `period_median` (alias `rolling_median`), `period_avg`, `period_sum`, `hit_rate`, `streak`, `period_stdev`, `period_var`, `period_cv`, `period_range`, `period_count`, `miss_rate`, `miss_streak`, `longest_streak`, `cagr`, `slope`, `mad`, `projection`.

Host names fold onto YAML keys (case, spaces, underscores; measure keys also compact-fold). Do not create two YAML keys that collide after fold.

---

## Ownership

**YAML owns calculation.** Measure `op` / `agg` / `fn` / `expr` do not change with grain.

**YAML owns the grouping allowlist and cut variants.** `dimensions:`, `default_dimensions`, extras-only `cuts[].group_by`, `exclude_from_grain`.

**The request owns only which allowlisted dims are active** (`context.selected_dimensions`). That is GROUP BY keys, not math. Do not declare `parameters.selected_dimensions`.

### Breaking changes

- `default_dimensions` is required (`[]` = worldwide default). `cuts[].group_by` is extras only.
- Rows include `grouped_dimensions`. Result filters on dims not in that cut grain skip (`not_in_grain`).
- Payload adds `selected_dimensions`, `applied_cuts`, `dropped_cuts`, `dropped_groups`, `grain_warnings`.

---

## 1. Two files, two jobs

| File | Job | Never put here |
|---|---|---|
| `kpi_config/models/<kpi_group>/<model_id>.yaml` | What DuckDB reads (physical tables/joins or a SQL/CTE extract) | KPI math, `agg:`, `op:`, `expr:` |
| `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml` | Time, dimensions, base facts, cuts, requestable measures | ADLS paths, Python, free SQL |

**KPI YAML never becomes DuckDB SQL for measure math.** DuckDB retrieves physical columns (time, dimensions, and columns the row pipeline walks to). Pandas then builds every base fact and every requested measure. A `kind: sql` model is optional **per KPI** to shape this extract (joins, filters, optional SQL windows). There is no ban on `SUM(` / `LAG(` in model SQL.

**`model:` on the KPI must equal `model_id:` on the model file.** Folding covers case, spaces, and underscores (`Sotif` = `sotif`). It does **not** treat `sotif` and `sotif_sql` as the same id. If the model file says `model_id: sotif_sql`, the KPI must say `model: sotif_sql`.

---

## 2. How a request is calculated (so YAML makes sense)

1. The host sends `execution.kpi_id` and a list of keys (`measures_required` or `measures_requested`). If the host **omits** that field and YAML has `meta.SelectedMetrics`, those keys are the projection. An explicit `[]` still computes **nothing**.
2. Those keys must exist under `measures:`. An empty list does not run the whole catalog.
3. The engine walks dependencies (`of:`, `left`/`right`, `inputs:`, `expr:`, `green_when.of`, and `having.of`) and extracts only the base facts that graph needs.
4. Time parts are independent predicates. Together they define a selection S; `anchor = max(S)`. A scalar `time.filter_code` on the context is still one value. Missing parts are not applied. Never `WHERE month IN (...)`. `parameters.time_grain` may pick an allowlisted interval when the KPI declares that parameter; lookback widens the scan from the requested graph (`previous_year_value` → 12 months).
5. DuckDB returns physical columns. `filters:` with `apply: extract` (and undeclared `IN` lists) sit on that query. Pandas stable-sorts, topo-applies named row steps (`expr:` / `lookup:` / `over:`), folds `agg:`, densifies, then combo measures. `having:` drops groups; optional `then_group_by` re-folds survivors; cut ops run on what remains. `apply: result` is dim-only after that. Hosts send `context.selected_dimensions` to pick allowlisted GROUP BY keys; YAML measure math stays the same.

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
| Nested `+ - * /`, CASE, or `coalesce(` on retrieved columns | `base_measures` + named `expr:` steps | SQL `SUM()` / subqueries in KPI YAML |
| A static code→value map | `lookup:` on a base | Giant CASE |
| Per-customer / per-entity sequence, lag, running sum | `over:` on the pre-fold detail | Calendar `op: lag` (that is vs the anchor on the densified spine) |
| Drop groups below a measure floor | KPI `having:` | `apply: result` (dim-only) or emitting a 1/0 flag |
| Flag healthy vs drop unhealthy | `op: predicate` vs `having:` | Multiplying two threshold flags as a substitute for AND |
| One period’s value (current, last year, last quarter) | `measures` + `op: point` | A window of length 1 unless you really want window null/zero rules |
| Trailing / leading / YTD / QTD total or avg | `op: window` (`range: qtd` is quarter-to-date, not trailing 3) | Summing several `point` measures by hand |
| A graph series | `op: trend` (axis in `trend_axes`, English labels in `trend_labels`) | Returning many `point` keys |
| Same KPI at day / week / month | `time.grains` + `data_points` map; host sends `parameters.time_grain` | Changing `trailing: { months: 3 }` to mean “3 weeks” |
| Positive / Negative / Neutral | `op: fn` + `fn: sign_label` | A `green` flag (that is `green_when`) |
| Row is on the good side of a bar | top-level `green_when` | `sign_label` or a host-side compare |
| Default measure list when the host sends no keys | `meta.SelectedMetrics` | Treating explicit `[]` as “run the catalog” |
| YoY, ratio, share, add/sub of **measures on the same row** | `op: arithmetic` or `op: fn` | Repeating the same formula on `base_measures` |
| This combo vs **all groups on this cut** (`SUM() OVER ()`) | `op: percent_of_total` | `fn: percent` (that only sees this row) |
| Share **within** a parent dimension | `op: percent_of_total` + `partition_by:` | A second `group_by` on the measure (that is the cut) |
| Rank reasons / regions | `op: rank` (same shape as `percent_of_total`) | Sorting in the host after the fact if you need engine ranks |
| Nested formula over **other measures** | `op: expr` | `base_measures.expr` (that is per-row, then aggregated) |
| A fixed target / goal | `op: constant` | Hard-coding the number in every `expr` |
| Quartiles, Pareto, running totals, vs-leader | add-on cut ops (`ntile`, `cumulative_share`, `gap_to_leader`, …) | Hand-ranking in the host |
| Same measure at another period | `op: lag` / `lead` / `diff` / `pct_change` / `index` | A second `point` plus arithmetic when a period op already exists |
| Series stats (EWMA, hit rate, CAGR) | `op: hook` + a name from [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) | Editing `pipeline/` per KPI |
| Echo a dimension as a `measure_key` | `op: dimension` | A fake numeric measure |
| IF / NULLIF / IS NULL as a value | `op:` / `fn:` (`if_else`, `nullif`, `zero_if_null`, …) or `expr:` CASE | SQL `CASE` in DuckDB |
| Math no listed kind can express (iterative, custom allocation) | new hook under `capabilities/hooks/` + `registries/hooks.yaml` | `eval()` or import paths from YAML |

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
| Custom row math you will reuse | Add a **column function** (capabilities + registries), then `op:` |
| Custom scalar math you will reuse | Add a **measure function** (capabilities + registries), then `op: fn` |
| Needs the whole densified series | Add a **hook** (capabilities + registries), then `op: hook` |

---

## 4. Skeleton — copy and fill

```yaml
kpi_id: 3004                 # must match execution.kpi_id
version: 1
model: sotif                 # MUST equal models/<file>.yaml model_id:

time:
  column: event_month        # date column on the extract
  grain: month               # day | week | month | quarter | year
  # source_grain: day        # stored grain of the column; omit = same as grain
  # grains: [day, week, month]   # allowlist for parameters.time_grain
  filter_code: reporting_month
  calendar: gregorian        # gregorian | fiscal
  # format: yyyymm           # if the column / filter is 202607, not 2026-07

# data_points: 12            # scalar only when grains is omitted or a single grain
# data_points: { day: 30, week: 12, month: 12 }

# meta:
#   KPI: Sotif
#   ParentKPI: Quality
#   IsChild: false
#   SelectedMetrics: [current_value, yoy_month, trend_n]

# green_when:
#   above: 0.98              # or below: 5 — not both
#   of: current_value

dimensions:
  - { name: reason_code, from: reason_code }
  - { name: region, from: region }

default_dimensions: [reason_code]

base_measures:
  sotif_value:
    sql: amount
    agg: sum

cuts:
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

measures:
  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }
```

Required:

- `kpi_id`, `model`, at least one `cut`, at least one `measure`, `default_dimensions`
- Either `time:` (period KPIs) or omit it entirely (snapshot KPIs)
- Every host `measure_key` declared under `measures:`
- `ParentKPI` / `IsChild` / `KPI` are echo-only; do not point `ParentKPI` at another YAML file
- `cuts[].group_by` lists extras only (not names already in `default_dimensions`)

---

## 5. `time`

| Key | Values | Default | When to set |
|---|---|---|---|
| `column` | identifier | required if `time:` is present | The date/timestamp DuckDB retrieves |
| `grain` | `day`, `week`, `month`, `quarter`, `year` | `month` | Default period every measure counts in. `week` is ISO Monday |
| `source_grain` | same set as `grain` | `time.grain` | Stored grain of the time column. A pick **finer** than this fails (day < week < month < quarter < year). Monthly facts cannot become daily or weekly |
| `grains` | list of grains | omitted = `{grain}` only | Allowlist for `parameters.time_grain`. `time.grain` must appear in the list |
| `filter_code` | context filter key | required unless `periods:` or `compose:` is set | Scalar selected period. Not hardcoded to `reporting_month`. Wins outright if present on the context |
| `periods` | `{ year: "year", month: "current month" }` | omitted | Independent part predicates. Values are integers (`3`, `"03"`). Mutually exclusive with `compose:` |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal changes `quarter` and `year` only. `week` is ISO-only |
| `fiscal_start_month` | 1–12 | `4` | Only when `calendar: fiscal` |
| `format` | see below | ISO `YYYY-MM` / `YYYY-MM-DD` | When the column or filter is not ISO |
| `compose` | `{ template: "{year}/{month:02}" }` | omitted | **Superseded by `periods:`.** Concat segregated context keys; cannot be set with `periods:` |

**`format` aliases:** `yyyy-mm-dd`, `yyyy-mm`, `yyyy/mm`, `yyyymmdd`, `yyyymm`, `mmyyyy`, `mm-yyyy`, `dd-mm-yyyy`, `dd/mm/yyyy`, or a strptime string (`%d/%m/%Y`).

```yaml
time:
  column: event_month
  grain: month
  calendar: gregorian
  filter_code: reporting_month     # optional; wins if already on the context
  periods:
    year: year
    quarter: quarter
    month: "current month"
```

Prefer `periods:` when the host sends independent year / month / quarter keys. Lists are a union. `year=2026` alone is the full year.

```yaml
time:
  column: current_month
  grain: month
  filter_code: reporting_month
  format: yyyymm
  compose:
    template: "{year}{month:02}"   # host sent year + month; source is 202607
```

`compose` is superseded by `periods:` and cannot be set together with it. `time.format` parses the **composed** string. Lookback still widens from requested measures. The `year` / `month` keys are removed so they are not leftover `IN` filters. If `reporting_month` is already on the context, that scalar wins.

```yaml
time:
  column: event_date
  grain: month
  source_grain: day          # facts are daily; omit = same as grain
  grains: [day, week, month]
  filter_code: reporting_month
```

The host picks with `parameters.time_grain` (not a filter, not `execution`). The KPI must declare `parameters.time_grain`. Missing → YAML `default` or `time.grain`. Not in `time.grains` (or not equal to `grain` when `grains` is omitted) → bind error. A pick finer than `source_grain` also fails. After bind, plan / DuckDB bucket / densify / `trailing.periods` use the pick. Response `parameters.time_grain` is the effective grain; bound request values are `request_parameters`.

Top-level `data_points` is the reusable length for `trailing: { from: data_points }`. Use a **scalar** only when `grains` is omitted or a single grain. More than one allowed grain → a map with a positive int for **every** listed grain.

```yaml
data_points:
  day: 30
  week: 12
  month: 12
```

Rules:

- Declared `periods` parts conjoin; a missing part is not applied. Lists are a union. Unparseable values (`"March"`) still error.
- A scalar `time.filter_code` on the context still carries **exactly one** value. Two values is not a range.
- Missing time filters on a time-based KPI are legal (whole history; the engine probes min/max). An empty selection returns null measures and a `notes` entry, not an exception.
- A day pick requires a full `YYYY-MM-DD` unless `time.format` says otherwise. Week / month / quarter / year may accept `YYYY-MM` and truncate to the pick.
- `week` buckets to ISO Monday (Python and DuckDB). Do not assume DuckDB `date_trunc('week')`.
- `time.timezone` is rejected. Convert in a `kind: sql` model if needed.
- Omit the whole `time:` block for a snapshot KPI. Then a measure may not use a nonzero `offset`, `trailing`, or any kind that requires time (`window`, `trend`, `lag`, period hooks, …). `point` + `offset: { months: 0 }` is allowed. `constant` + `trailing` is not. Host `reporting_month` (and other time-like leftover codes) skip with `reason: no_time`; other unmapped filters still fail.

### Request parameters

`context.parameters` is a sibling of `filters`. It is not `execution.*`. 3004 omits the block and must receive an omitted or empty object.

There are **four** overlays (not one grammar). Do not add `select:`, `use:`, or `{ from: param }` (the key `from` stays `trailing.from` / `dimension.from`).

1. Reserved `time_grain` — pick from `time.grains`
2. Reserved `output_cut` — start the `also_emit` walk at that cut. A YAML `default` does not lock. Sending it on the context does. `pack_also_emit: false` on the root emits only that cut. 3004 omits it so G still packs R.
3. `when: { param, cases, else }` on `model` / `measures.*` / `base_measures.*`
4. `from_param:` on the allowlist (model id string; trailing/offset ints; constant `value` int|float)

```yaml
parameters:
  time_grain: { type: string }          # reserved grain pick
  output_cut: { type: string, default: G, allowed: [G, R] }
  Level: { type: string, allowed: [G, Y, R], map: { Green: G } }
  lookback: { type: int, default: 3 }
  codes: { type: list, item: string, default: [G, Y] }
  flags: { type: dict, item: string }   # echo-only; not in expr / fn extras
```

`when:` always needs `else:`. Bind validates every `allowed` case when all `when.param` names are the same. Datasets match **aliases**, not model id. `load_kpi(id, parameters=)` binds the request before resolve — a later bind does not re-pick `when:` bodies. List params are only the right of `in` (`Level in codes` or `Level in ('G', 'Y')`). Expr uses `=` not `==`.

Scalar names still inject into measure `expr` / fn kwargs. Response `parameters` is the time plan (`anchor`, `time_grain`, `span_start`, `lookback_months`, `time_selection`); bound values are `request_parameters`. `time_selection` is `{grain, start, end, parts, anchor_source}` (`context` / `data` / `legacy`).

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
    grain: quarter                       # day | week | month | quarter | year
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
| `if_null` | | `value`, `fallback` | Value or fallback |
| `nullif` | | `value`, `sentinel` | Null when equal |
| `null_if_zero` / `zero_if_null` | | 1 column | `NULLIF(x, 0)` / null → 0 |
| `is_null` / `is_not_null` | | 1 column | 1 or 0 |
| `if_else` | | `cond`, `then`, `other` | Then if cond is nonzero |

`op: sum` reduces **across columns of one row**. `agg: sum` reduces **down the rows of a period**. A base measure often uses both.

`expr:` may use the same names as calls, plus `CASE WHEN … THEN … ELSE … END`, `IS NULL`, comparisons, `AND` / `OR` / `NOT`, `NULL`, and `'strings'`. `CASE WHEN status = 'O' THEN amount ELSE 0 END` is per-row; use `where:` when unmatched rows should not contribute. String CASE is `expr:` only — `columns:` + `op:` is still numeric.

Unknown `op`, wrong arity, or a bad parameter name fails at **bind** and lists the registered names.

### 7.4 `expr:` on a base measure

Allowed grammar: identifiers, numbers, `+ - * /`, parentheses, allowlisted calls, `CASE WHEN … THEN … ELSE … END`, comparisons, `IS NULL` / `IS NOT NULL`, `AND` / `OR` / `NOT`, `NULL`, and single-quoted strings (`''` to escape). No comments, double quotes, or `;`. Calls must be names from the column registry (`coalesce`, `nullif`, …), not SQL `SUM()`.

```yaml
base_measures:
  harmonic:
    expr: (col_a * col_b) / (col_a + col_b)
    agg: sum
  open_amt:
    expr: CASE WHEN status = 'O' THEN amount ELSE 0 END
    agg: sum
```

Identifiers must be simple SQL names: `[A-Za-z_][A-Za-z0-9_]*`. `CASE`, `WHEN`, `THEN`, `ELSE`, `AND`, `OR`, `NOT`, `IS`, `NULL`, and `IN` are reserved. `END` is a CASE terminator; `end` is a legal column name outside CASE (`date_diff(start, end, 'day')`).

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
| `first` | no | null | null | First event in the period (text columns still coerce to numeric) |
| `last` | no | null | null | Snapshot balance (text columns still coerce to numeric) |
| `stddev` | no | null | null | Sample stdev (ddof=1); one row → null |
| `variance` | no | null | null | Sample variance (ddof=1); one row → null |
| `mode` | no | null | null | Most frequent value; ties → smallest |

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
      op: in                 # in | eq | ne | gt | gte | lt | lte | between
      values: [O]
```

`eq` / `ne` / `gt` / `gte` / `lt` / `lte` also accept `value:` (singular). `between` requires `values: [lo, hi]` (exactly two); a singular `value:` is BindError. Numeric ops (`gt`/`gte`/`lt`/`lte`/`between`) coerce the compare column; `in`/`eq`/`ne` stay identity so status codes remain strings. `ne` is SQL-style: a null in the compared column does **not** pass. `like` / `is_null` are not legal on base-measure `where:`.

The same `where:` shape (and `ignore_filters:`) is legal on a **measure** whose `of:` is a base: the binder clones that base with the extra mask. Filtering a derived `of:` is BindError.

### 7.7 Multi-model facts

```yaml
base_measures:
  defects:   { sql: defect_count, agg: sum }
  shipments: { sql: shipped_qty,  agg: sum, model: shipments }
```

If a requested measure graph reads more than one model, declare `model_relations` (see §11). Independent extracts in one KPI file do not need a join.

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
| `percent_of_total` | `of:` measure or base | scalar | This row / sum of rows on the cut × 100 |
| `dimension` | key ∈ `dimensions:` | attribute | Host sent a dim as a measure |
| `hook` | `hook:` + usually `of:` | scalar | Allowlisted series function (see the catalog) |
| add-on cut / period | kind-specific keys (`tiles`, `n`, `vs`, `offset`) | scalar | `ntile`, `lag`, `diff`, … — [CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) |

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

`offset` counts **backwards**. Units: `days`, `weeks`, `months`, `quarters`, `years` (they add: `{ years: 1, months: 2 }` = 14 months). Calendar keys keep that meaning after a grain pick (`offset: { weeks: 1 }` is always seven days). Missing period → `null`.

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
  range: ytd                 # year start → anchor (`cumulative` is an alias)

value_qtd:
  of: sotif_value
  op: window
  range: qtd                 # quarter start → anchor (not trailing 3)

value_next_3m:
  of: sotif_value
  op: window
  range: leading
  trailing: { months: 3 }
```

| `range` | Window |
|---|---|
| `trailing` (default) | last N vs the reference |
| `leading` | next N; needs `trailing:` |
| `mtd` / `qtd` / `ytd` / `wtd` | calendar period start → reference (`wtd` needs the **effective** grain `day`) |
| `full_month` / `full_quarter` / `full_year` | whole calendar period containing the reference |
| `cumulative` | alias of `ytd` |

`offset` on a window is backwards (same as `point`). Named PTD / `full_*` stay calendar when the pick changes (`qtd` is still quarter-to-date; month + `qtd` is valid). `wtd` binds when `day` is in `time.grains` and fails unless the pick is `day`. Named ranges cannot also set `trailing:`.

`inclusive: false` ends one period **before** the anchor.

`trailing` / `offset` keys `days`, `weeks`, `months`, `quarters`, `years` are **calendar** and do not change meaning when the pick changes (`trailing: { months: 3 }` on a week pick is three calendar months, not three weeks). `trailing: { periods: N }` and `trailing: { from: data_points }` count **picked grain** steps.

The window uses the base measure’s own `agg`.

### 8.3 `trend`

```yaml
trend_12m:
  of: sotif_value
  op: trend
  trailing: { months: 12 }   # always 12 calendar months
  inclusive: true
  cuts: [G]                  # default: default_cut only

trend_n:
  of: sotif_value
  op: trend
  trailing: { from: data_points }   # 30 / 12 / 12 by pick
```

Fixed-length array. Shared x-axis is `trend_axes` (ISO period starts) plus `trend_labels` (fixed English: `23 Mar`, `2026-W30`, `Jul 2026`, `2026-Q1`, `2026`). Same keys and lengths; two days in the same week stay distinct. Labels are not locale-dependent. Empty `sum`/`count` slots are `0`; others `null`. Cap: **50,000 cells** (rows × length) per cut. A day pick plus a large `data_points` widens the extract spine; the cap still applies.

`cuts:` is honoured for **trend**, **rank**, and **percent_of_total** only.

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
| `coalesce` / `if_null` | | 2+ / 2 | first non-null | null if all null |
| `nullif` / `null_if_zero` / `zero_if_null` | | 2 / 1 / 1 | null helpers | — |
| `is_null` / `is_not_null` / `if_else` | | 1 / 1 / 3 | flags and branch | `if_else` uses `other` when cond is 0 or null |
| `sign_label` | `change_direction` | `value` | `Positive` / `Negative` / `Neutral` | null stays null; `0` is Neutral |

`growth_pct` is a **ratio** (`0.05` = +5%). Use `percent` if the UI wants `5.0`.

A two-argument function given `of: [a, b, c]` is folded left to right.

### 8.5 `fn`

Same registry as `arithmetic`. Use `inputs:` (list or `{parameter: measure}`).

```yaml
yoy_growth:
  op: fn
  fn: growth_pct
  inputs: { current: current_value, previous: previous_year_value }

direction:
  op: fn
  fn: sign_label             # alias: change_direction
  inputs: [yoy_month]
  # params: { positive: Up, negative: Down, neutral: Flat }
```

`sign_label` is a leaf string. Null → null; `0` → Neutral; `> 0` → Positive; `< 0` → Negative. It is not the row `green` flag. String results do not feed arithmetic.

Cycles (`a → b → a`) fail at bind. Shared inputs are computed once.

### 8.6 `expr` (measure level)

```yaml
agg_ratio:
  op: expr
  expr: (a_value * b_value) / (a_value + b_value)
```

Same `+ - * / ( )` grammar. Identifiers are **measure keys**. Zero or null denominator → null.

### 8.7 `constant` / `rank` / `percent_of_total` / `dimension`

```yaml
target:
  op: constant
  value: 0.98

reason_code_rank:
  op: rank
  of: current_value          # or a base measure (treated as anchor point)
  partition_by: [reason_code]  # optional; omit = whole cut. group_by: still works
  order: desc                # desc (default) | asc
  cuts: [G]

percent_gt:
  op: percent_of_total
  of: current_value          # or a base measure
  cuts: [R]                  # grain is the cut's group_by, not a second GROUP BY

percent_within_site:
  op: percent_of_total
  of: current_value
  partition_by: [site_category]  # SUM() OVER (PARTITION BY site_category)
  cuts: [R]

reason_code:
  kind: dimension            # key must match dimensions:
```

**Cut `group_by` vs measure `partition_by`:** the cut decides which rows exist. `partition_by` only splits the window (`OVER ()` vs `OVER (PARTITION BY …)`). It does not add or drop rows. Omit `partition_by` for the stored-procedure `percent_gt` (share of every group on the cut). **Cookbook:** `partition_by` ⊆ that cut’s effective grain. Rank-within-category needs `product_category` on the grain (or BindError). G (worldwide) and R (by region) run having independently.

Rank uses Pandas `RANK()` (ties share; next rank skips). Null sources stay null. `percent_of_total` is `value * 100 / SUM(value)` on those rows **after having**; zero or null total → null. Neither can be `left` / `inputs` / `expr` of another measure in the same request. Cut ops accept `order_by:` dim columns after the measure sort.

Same YAML shape for every cut-wide op:

```yaml
<measure_key>:
  op: percent_of_total   # or rank
  of: <point or window measure>
  partition_by: []       # optional; omit = whole cut
  cuts: [R]              # optional; default = default_cut
```

### 8.8 `meta` and `green_when`

```yaml
meta:
  KPI: Sotif
  ParentKPI: Quality
  IsChild: false
  SelectedMetrics: [current_value, yoy_month, trend_n]

green_when:
  above: 0.98            # or below: 5 — not both
  of: current_value
```

`KPI` / `ParentKPI` / `IsChild` are copied onto the response. Do not validate `ParentKPI` as another YAML file.

`SelectedMetrics` is the default projection **only** when the host omits `measures_required` / `measures_requested`. Explicit `[]` still computes nothing.

`green_when` needs exactly one of `above:` or `below:`, plus `of:`. `green` is `true` when the named measure is `>= above` or `<= below`; `false` when the compare runs and fails; `null` when the value is null. `of` is always added to the resolved graph, so a request that left `current_value` off the list still computes it for `green` (it may appear on the row). Response `meta` copies YAML meta plus `{above|below, of}`. `green` is not `sign_label`.

---

## 9. `cuts` and `row_set`

A cut is a grouping **variant**. `group_by` lists extras only. Effective grain = `selected_dimensions` (or `default_dimensions`) minus `exclude_from_grain`, then extras.

```yaml
default_dimensions: [reason_code]
cuts:
  - name: G
    group_by: []
    exclude_from_grain: [region]
    ignore_filters: [region]
    also_emit: [R]
  - name: R
    group_by: [region]
    ignore_filters: []

default_cut: G
row_set: span_union
```

| Key | Meaning |
|---|---|
| `group_by` | Extra dimensions this cut always adds (disjoint from `default_dimensions`) |
| `exclude_from_grain` | Request dims this cut drops; couple to dim-named `ignore_filters` |
| `ignore_filters` | Cuts that skip this **calc** filter (G worldwide / R filtered). Legal with default `apply: extract` (runtime defers). Not valid with `apply: result` |
| `also_emit` | Extra cuts in the same response |
| `row_set: span_union` | Row if the combo has data **anywhere in the scan** (until the densify cell cap) |
| `row_set: anchor_only` | Row only if it has data **at the selected period** |

Use `anchor_only` when the page should list only what is active now.

---

## 10. Filters

YEAR / MONTH is `time.periods` (independent predicates) or `time.filter_code` (one period). `compose.template` still concatenates year+month but is superseded. Everything else is a **row filter**: the host sends values; YAML says how.

```yaml
filters:
  effective_day:
    column: day
    op: lte              # DAY <= @EffectiveDay
    apply: extract       # DuckDB WHERE — cheapest; SUM / percent_of_total are correct
  region:
    column: region
    op: in
    apply: extract       # legal with ignore_filters; runtime defers when G is emitted
  reason_code:
    column: reason_code
    op: in
    apply: result        # hide JSON rows; LATE's share still includes OTHER
```

| Need | `apply` |
|---|---|
| Fiscal year + month | `time:` — not this block |
| `DAY <= EffectiveDay` on facts | **extract** (or calc if `day` is Pandas-only) |
| Region IN, all cuts the same | **extract** |
| Region IN, G ignores it | **extract** (or calc) + `ignore_filters` |
| Hide reasons in JSON, keep full share | **result** |

You normally declare nothing for plain `IN` lists. Context `filter_column_mappings` or a matching column name is enough (extract, unless a cut ignores the code).

```yaml
filter_map:
  plant_code: region         # this KPI only; wins over context mappings
```

- Unmapped filter **with values** → hard error (never dropped). Unmapped `[]` → skip (`skipped_filters`).
- Empty `in` list, omitted key, or all-null → **skip** (not `FALSE`). Breaking vs matching nothing. `[""]` is a real value.
- All row filters are optional. `optional: true` is ignored; `optional: false` is a bind error.
- `is_null` / `is_not_null` apply when the key is present; omit the key to skip.
- `input_text: heir` / `hier` is rejected; expand hierarchies in the context builder.
- Values are SQL parameters; nothing is concatenated into SQL text.
- Ops: `in`, `eq` (`==`), `ne` (`<>`), `lt`/`lte`/`gt`/`gte`, `like`/`ilike`/`not_like`, `between`/`not_between`, `is_null`/`is_not_null`. Host supplies LIKE `%` / `_`.
- Segregated keys for a **non-time** column: `filters.*.compose.template` (`"{a}_{b}"`). The time column always uses `time.compose` so windows widen.

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

Two models with no `model_relations` is an error only when a requested graph spans them (not a cross join). Requesting one extract at a time is allowed. Cuts live on the KPI; do not set `model:` on cuts or dimensions.

---

## 12. Model YAML (what DuckDB reads)

File: `kpi_config/models/<kpi_group>/<model_id>.yaml` (or flat `models/<model_id>.yaml`). The KPI `model:` value must match `model_id:`. Group folders are authoring only; ids stay unique.

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

**Prefer reuse.** Read [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md) (generated from the YAML registries) or call `kpi_engine.list_capabilities()`. Add-on kinds (`ntile`, `lag`, …) and hooks (`ewma`, `hit_rate`, …) are listed there with `role: addon`.

Decision tree:

1. Enabled name already in the registries? → KPI YAML only. Do not open `pipeline/`.
2. New column math? → append in `capabilities/functions/column/impl.py` + a key in `registries/functions/column.yaml` (`role: addon`).
3. New scalar math? → `capabilities/functions/measure/impl.py` + `registries/functions/measure.yaml`.
4. One-group custom series logic? → `capabilities/hooks/impl.py` + `registries/hooks.yaml` (`requires_value` / `extra_keys` if needed).
5. New cut/combo/period shape? → `OpPlugin` in `capabilities/ops/` + `registries/ops.yaml`. Not a `pipeline/` edit.
6. New filter operator, agg, time format, common YAML field, or pipeline stage? → platform work in `pipeline/`. Escalate.

YAML may only name allowlisted entries — never a dotted import path. `compute(context)` stays the only entry point.

`enabled: false` on an **add-on** turns it off for the whole process (restart required). Run `kpi_engine.pipeline.loader.impact_check("name")` first (lists `kpi_config/kpis/` and test YAML that mention it). Platform names (`point`, `window`, `fn`, `hook`, shipped functions) cannot be disabled.

### Column function — `base_measures.op`

Receives one numeric pandas Series per `columns:` entry; must return a Series of the same length.

```python
# capabilities/functions/column/impl.py
def weighted_score(hits, weight):
    return hits * weight * 10
```

```yaml
# registries/functions/column.yaml
weighted_score:
  role: addon
  enabled: true
  description: Weighted score of hits × weight × 10.
  example: |
    score:
      columns: { hits: ontime, weight: fullqty }
      op: weighted_score
  module: kpi_engine.capabilities.functions.column.impl
  attr: weighted_score
```

```yaml
base_measures:
  score:
    columns: { hits: ontime, weight: fullqty }
    op: weighted_score
    agg: sum
```

### Measure function — `measures.fn` / `arithmetic`

Same pattern under `capabilities/functions/measure/` and `registries/functions/measure.yaml`.

### Hook — last resort

Needs the densified period series. Add the function in `capabilities/hooks/impl.py` and a key in `registries/hooks.yaml`. Declare `offset:` or `trailing:` so the planner scans enough history. Must not open DuckDB or read storage.

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
- Empty `measures_required` / `measures_requested` (`[]`) computes nothing. It does not expand to every YAML measure. `meta.SelectedMetrics` applies only when the host **omits** the field.
- Host keys fold onto YAML keys; unknown keys fail at bind with the valid list.
- KPI YAML cannot reference another KPI’s measures. `meta.ParentKPI` is echo-only.
- Calculation controls go in `context.parameters` (declared in YAML). They are not filters and not `execution.*`. A KPI with no `parameters:` block rejects a non-empty object.

**SQL vs Pandas**

- YAML/Pandas is the complete default for measure math after retrieve. A `kind: sql` model is opt-in **per KPI** to shape this extract (joins, filters, optional SQL windows). There is no grep ban on `SUM(` / `LAG(` in model SQL.
- KPI `sql:` / `expr:` never enter DuckDB. No `SUM()`, comments, or quotes in KPI formulas.
- Expressions allow `+ - * / ( )`, CASE, comparisons, `IS NULL`, `AND`/`OR`/`NOT`, `NULL`, `'strings'`, and allowlisted calls (`date_diff`, `round`, …). Comments, double quotes, `;`, and unknown function names are rejected.
- Identifiers must match `[A-Za-z_][A-Za-z0-9_]*` (no dots, hyphens, or quoted names).
- `lookup:` / `over:` / `expr:` / `sql:` / `columns:`+`op` are mutually exclusive on one base.
- Calendar `op: lag` is vs the densified spine and the **anchor**. Entity windows are `over:` on pre-fold detail. `op: lag` cannot set `partition_by`.
- `over.partition_by` / cut `partition_by` must be a subset of that cut’s **effective grain**. Existing BindErrors stay.
- Row helpers (`expr`/`lookup`/`over` without `agg:`) are `measures.of` only at `identity_grain` (every emitted cut must match). Otherwise fold with `agg:` or keep them as named steps.
- `kind: sql` retrieve SELECTs walked columns, not the full parquet list. Walked names must appear on the CTE / `output_schema`.
- Zero-filled sparse groups (densify / `fill_zero`) can fail `having` `gt: 0`. HAVING is not a second pass.

**Product boundary (not a later engine version)**

- Regex, JSON, geospatial, ML, timezone conversion, result caching, hierarchy expansion, and cross-KPI measure references stay **hooks**, `kind: sql`, or the host. The closed catalog is existing ops/fns plus named steps / lookup / over / having / predicate / date+numeric fns.
- Host ADLS, auth, jobs, and the context builder stay outside `compute(context)`.
- Extracts larger than `OVER_ROW_CAP` / densify `TREND_CELL_CAP` fail fast. Mitigation is narrower filters, coarser retrieve, or a SQL model that pre-aggregates — not a distributed Pandas rewrite.

**Time**

- No timezone conversion; `time.timezone` is rejected.
- `calendar: fiscal` affects `quarter` and `year` only. Fiscal months are calendar months. There is no fiscal-week grain.
- `time.anchor: last_observed` (opt-in) clamps the anchor to the last observed bucket; default remains selection end. `time.max_span_years` is a hard cap after the span is final.
- Snapshot KPIs (no `time:`) cannot use windows, trends, nonzero offsets, trailing, or time-requiring add-ons (`lag`, period hooks, …).
- `trailing` / `offset` calendar keys (`days`, `weeks`, `months`, `quarters`, `years`) keep that meaning after `parameters.time_grain` changes. `periods` and `from: data_points` follow the pick. Month names (`March`/`Mar`) are accepted for the month part only.
- A pick finer than `time.source_grain` is rejected. Monthly facts cannot become daily or weekly.
- `wtd` needs the effective grain `day`. Other named PTD / `full_*` ranges stay valid on a coarser pick.
- Multi-grain KPIs require a `data_points` map covering every listed grain. A day pick plus a large `data_points` widens the spine; the 50,000 trend-cell cap still applies.

**Cuts and payload**

- `measures.*.cuts` restricts **trend**, **rank**, and **percent_of_total** only; other ops ignore it.
- Trends default to `default_cut` only (high-cardinality trends explode the payload). `offset:` on a trend shifts the axis; do not `lag { of: trend }`.
- Rank a lagged measure (`rank of lag`); do not `lag { of: rank }`.
- `percent_of_total.versus_cut` takes another declared cut's total as the denominator. Arithmetic/fn/expr may consume rank and percent_of_total.
- Trend cells capped at 50,000 per cut.
- Physical joins: `inner`, `left`, `right` only.

**Filters and context**

- Hierarchies (`input_text: heir`) are not expanded here.
- `business_date` is ignored.
- Unmapped filters error; they are never silently dropped.
- YEAR / MONTH is `time.periods` (independent predicates), `time.filter_code` (one scalar), or `time.compose.template` (concat year+month, superseded). Other predicates use `filters:` (`extract` / `calc` / `result`). A measure whose `of:` is a base may add `where:` / `ignore_filters:`.
- Exactly one `execution.view_details` entry.

**Aggregation**

- Non-additive aggs (`count_distinct`, `median`, `percentile`, `first`, `last`, `stddev`, `variance`, `mode`) re-read row-level data.
- `percentile` requires `percentile:`.
- Pandas fold of a column `op` only supports `sum`, `avg`, `count`, `min`, `max`, `first`, `last` when collapsing detail; non-additive names stay on the row path.

**Extensions**

- Custom logic is allowlisted in `registries/` (impl under `capabilities/`). Dotted import paths and `context.udf.module_path` are rejected.

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
| `parameters.time_grain … is not allowed` | Send a grain from `time.grains`, or omit the pick |
| `execution.time_grain is not supported` | Move the pick to `context.parameters.time_grain` |
| `This KPI declares no parameters` | Omit `context.parameters`, or add a YAML `parameters:` block |
| `finer than time.source_grain` | Do not pick day/week on monthly facts; raise `source_grain` or drop the pick |
| `time.grain=day requires a full date` | Send `YYYY-MM-DD` when the pick is day |
| `data_points must be a map` | Multi-`grains` needs `{ day: N, week: N, … }` covering every listed grain |
| `range=wtd needs time.grain: day` | Allow `day` in `time.grains` and pick day, or drop `wtd` |
| `green_when needs exactly one of above: or below:` | Set one threshold, not both |
| `time.periods.day is finer than time.grain` | Drop the finer part, or lower `time.grain` |
| `time.periods and time.compose.template cannot both be set` | Keep one |
| `Cannot parse month 'March'` | Send `3` or `"03"` |
| `Month filter must contain exactly one value` | Scalar `filter_code` is one period; use `periods:` for lists |
| `Filter '…' has no column mapping` | Add context mapping or `filter_map` |
| `Unknown filter op` | Use a name from §10 (`lte`, `like`, `between`, …) |
| `apply: result cannot be listed in ignore_filters` | Use `apply: calc` + `ignore_filters` |
| `optional: false is not supported` | Row filters cannot be required |
| `Compose placeholder 'year' is missing` | Send every `{placeholder}` on the context, or send `time.filter_code` as one scalar |
| `compose.template must name at least two` | Two or more `{filter}` placeholders |
| `Illegal measure sql` / unknown function | Use CASE or an allowlisted call; put `SUM` in `agg:` |
| `unknown op` / `unknown fn` | Use a name from §7.3 / §8.4, or register one |
| `op: percent_of_cut_total` | Use `op: percent_of_total` |
| `percent_of_total requires of:` | Point `of:` at a measure or base |
| `partition_by '…' is not a cut group_by` | Use a dimension / cut grouping name |
| `uses expr: and cannot also set columns/op/sql` | Pick one style per base measure |
| `measures.<k> spans models` | Add `model_relations`, or request one extract |
| `cuts '…' is not on this extract` | Point `cuts:` at a grain this model has |
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
  trend_12m:           { of: sotif_value, op: trend, trailing: { months: 12 }, cuts: [G] }
```

### Day / week / month switch with labels and green

```yaml
kpi_id: 3011
model: sotif
time:
  column: event_date
  grain: month
  source_grain: day
  grains: [day, week, month]
  filter_code: reporting_month
data_points:
  day: 30
  week: 12
  month: 12
meta:
  KPI: Sotif
  ParentKPI: Quality
  IsChild: false
  SelectedMetrics: [current_value, yoy_month, trend_n]
green_when:
  above: 0.98
  of: current_value
dimensions:
  - { name: reason_code, from: reason_code }
base_measures:
  sotif_value: { sql: amount, agg: sum }
default_dimensions: [reason_code]
cuts:
  - { name: G, group_by: [], ignore_filters: [] }
default_cut: G
measures:
  current_value: { of: sotif_value, op: point, offset: { months: 0 } }
  previous_year_value: { of: sotif_value, op: point, offset: { years: 1 } }
  value_3m:      { of: sotif_value, op: window, trailing: { months: 3 } }   # always 3 calendar months
  value_qtd:     { of: sotif_value, op: window, range: qtd }
  yoy_month:     { op: arithmetic, fn: growth_pct, left: current_value, right: previous_year_value }
  direction:     { op: fn, fn: sign_label, inputs: [yoy_month] }
  trend_n:       { of: sotif_value, op: trend, trailing: { from: data_points }, cuts: [G] }
```

Host sends `parameters.time_grain: week` (or omits it to keep `month` when the YAML default / `time.grain` is month). Chart labels come back in `trend_labels`, not as measures.

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

### Share of all groups on a cut (`percent_gt`)

```yaml
default_dimensions: [reason_code]
cuts:
  - name: R
    group_by: [site_category]
default_cut: R
measures:
  current_value: { of: numerator, op: point, offset: { months: 0 } }
  percent_gt:
    op: percent_of_total
    of: current_value
    cuts: [R]
```

Grain is the cut. Omit `partition_by` so each row is `current_value / SUM(current_value on R) * 100`. Use `fn: percent` only when both operands are on the **same row**.
