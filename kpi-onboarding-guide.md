# KPI onboarding guide

This is the playbook for adding a KPI to the engine, and for deciding **which files to change** when the calculation is new.

Related docs:

- [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) — **live list of every op, function, and hook**
- [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md) — **write a KPI YAML: functions, columns vs expressions, when to use what, limits**
- [kpi-yaml-reference.md](kpi-yaml-reference.md) — **every YAML key, op and aggregation the engine supports**
- [README.md](README.md) — folders, install, YAML meaning
- [kpi-framework-plan.md](kpi-framework-plan.md) — architecture and locked decisions

This guide is the *process*: what to confirm, what to write, what to change. For the live name list, keep [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) open. For YAML keys, keep the reference open beside it.

The engine has three roots under `udfs/kpi_engine/`:

| Folder | Role |
|---|---|
| `core/` | Frozen pipeline (adapt, bind, extract, dispatch). Not for a new catalog name. |
| `capabilities/` | Bodies of ops, column/measure functions, and hooks. |
| `registries/` | YAML allowlist. A name is callable only if it is listed here. |

---

## 1. What you are onboarding

A KPI in this framework is:

| Piece | Lives in | Role |
|---|---|---|
| Context JSON | Existing metadata framework (you do not change it) | `kpi_id`, filters, dataset paths, `measures_required` |
| Model | `udfs/config/models/<name>.yaml` | What DuckDB reads (tables/joins or SQL) |
| KPI definition | `udfs/config/kpis/<kpi_id>.yaml` | Dimensions, base fact, cuts, calculated measures |
| Engine | `kpi_engine/` | Reusable; do not fork it per KPI |

The UI asks for columns by **`measure_key`**. Those names must exist under **`measures:`** in the KPI YAML.

---

## 2. Standard onboarding (YAML only)

Use this path when the math already exists in [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md): **point**, **window**, **trend**, **arithmetic** / **fn** / **expr**, **rank**, **percent_of_total**, add-on kinds (`lag`, `ntile`, …), and allowlisted hooks.

### Step 1 — Confirm the context

From the metadata team (or a sample request), note:

1. `execution.kpi_id` (file name will be this number/string).
2. Dataset **alias** (e.g. `sotif`) and that `path` / `table_type` will be on the context.
3. Filter codes, especially the **selected month** (`time.filter_code`, e.g. `reporting_month`).
4. `measures_required[].measure_key` the page will send.
5. `filter_column_mappings` so every non-month filter binds to a column.

Do **not** put ADLS paths in YAML. Paths stay on the context.

### Step 2 — Model (only if this source is new)

- If the KPI uses an **existing** model (same tables as 3004): set `model: sotif` (or the existing `model_id`). **Do not copy the model file.**
- If the source is **new tables/joins**: add `udfs/config/models/<model_id>.yaml`.
  - `kind: physical` — `required_aliases`, `sources`, optional `joins`.
  - `kind: sql` — CTE string; declare `output_schema`; use `$alias_path` for paths.
- If the source is a **complex CTE**: prefer `kind: sql`. Do not encode that CTE in the KPI file.

### Step 3 — Copy a KPI file

```bash
cp udfs/config/kpis/3004.yaml udfs/config/kpis/<kpi_id>.yaml
```

Set `kpi_id` and `model` to match.

### Step 4 — Fill the four YAML blocks

**`time`**

- `column` — date/timestamp on the extract.
- `grain` — `day`, `month`, `quarter`, or `year`.
- `calendar` — `gregorian` (default) or `fiscal` with `fiscal_start_month` (default 4). Fiscal affects quarter and year grains only.
- `filter_code` — **exact** context filter key for the user-selected period. This is per KPI (not always `reporting_month`). Omit the whole `time:` block if the KPI has no period column.  
  That filter is the **anchor**. It must never be applied as `IN (one month)`.

**`dimensions`**

- Attributes that split rows (`region`, `reason_code`, …). Not numbers.

**`base_measures`**

- Internal fact: `sql: <column>` + `agg`.
- Built-in aggregations: `sum`, `avg`, `count`, `min`, `max`, `count_distinct`, `median`, `percentile`.
- `avg` is weighted; `min`/`max` are recomputed at each cut; the last three are non-additive and re-read row-level data. See [the reference](kpi-yaml-reference.md#4-base_measures--the-built-in-aggregations).
- The UI usually does **not** request this name. Calculated measures use `of: <this name>`.

**`cuts`**

- One grouping grain each (`group_by`, `ignore_filters`, optional `also_emit`).
- Example: **G** = no region, ignore region filter, also emit **R**.
- `default_cut` — used when context does not pick a level.

**`measures`** (this is what `measure_key` selects)

| `op` | When to use | Required fields |
|---|---|---|
| `dimension` | Context still sends a dimension as `measure_key` | `kind: dimension` |
| `point` | One period (current, previous year) | `of`, `offset` |
| `window` | Trailing N, QTD/YTD/MTD, or a full period as **one number** | `of`, `trailing:` or `range:` |
| `trend` | Graph: array of monthly values | `of`, `trailing.months`, optional `cuts` |
| `arithmetic` / `fn` / `expr` | YoY, ratio, or a formula over other measures | `fn` + `left`/`right` or `inputs:` or `expr:` |
| `constant` | A literal target / goal | `value` |
| `rank` / `percent_of_total` | Rank or share of groups on a cut | `of`, optional `partition_by`, `cuts` |
| `hook` | Logic no listed kind can express | `hook` (an allowlisted name), plus `offset`/`trailing` for lookback |
| other names | Cut / period / series add-ons (`lag`, `ntile`, `ewma`, …) | See [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) |

Every `measure_key` the page can send must appear here. Unknown keys fail at bind time.

Optional top-level keys: `row_set` (`span_union` default, or `anchor_only`), `filter_map` (filter code → column), and `model_relations` (join two extracts when a requested graph spans models). Cuts and dimensions stay on the KPI — do not set `model:` on them.

### Step 5 — Align metadata

- `measures_required` only lists keys from `measures:`.
- Month filter name = `time.filter_code`.
- Non-month filters have `filter_column_mappings` (or `filter_map` in YAML).
- `input_text: heir` is **rejected** until the context builder expands leaves.

### Step 6 — Validate, then test

```python
from kpi_engine import validate, compute

validate(sample_context)   # compile SQL, no scan
compute(sample_context)    # full JSON
```

Add a test under `tests/` with local parquet (see `tests/conftest.py`). Do not hit ADLS in unit tests.

### Step 7 — Entry point

Call `udfs.kpi_engine.main` (file `udfs/kpi_engine/main.py`). **Do not** add `udfs/<kpi>.py` per KPI. Routing is by `kpi_id`. Copy the whole `udfs/` folder (`kpi_engine`, `config`) into the host.

DuckDB/ADLS stay on the platform. Set `kpi_engine.platform.HOST_DUCKDB_GETTER` to the existing helper (`module.path:function_name`) when you copy this tree in. The engine reuses that session and does not `duckdb.connect()` in production. Context `datasets.*.table_type` chooses `delta_scan` (DELTA) or `read_parquet` (PARQUET). In SQL models prefer `$alias_scan` so the same YAML works for both.

Each `compute` / `validate` writes `logs/kpi-<kind>-<kpi_id>-<timestamp>.log` with every step, the full DuckDB SQL (including values inlined), and function invoke/return. Override with `log_dir=`; disable with `KPI_ENGINE_LOG=0`.

---

## 3. Checklist (standard KPI)

- [ ] `udfs/config/kpis/<kpi_id>.yaml` exists; `kpi_id` matches context
- [ ] `model` points at a model whose aliases exist on the context
- [ ] `time.filter_code` matches the selected-month filter
- [ ] `base_measures` column exists on the extract
- [ ] Every UI `measure_key` is under `measures:`
- [ ] Cuts `group_by` only list real dimensions
- [ ] `validate(context)` succeeds
- [ ] A local parquet test covers at least one lookback (if you use previous year / 3m)

**Do not change** `kpi_engine/core/` for this path.

---

## 4. New scenario or new calculation — what to change

Work **top-down**. Stop at the first row that fits.

### 4.1 Same catalog, different KPI

**Examples:** another fact with SUM, 3m/12m, YoY, G/R, a 12-month graph.

| Change | Do not change |
|---|---|
| `udfs/config/kpis/<id>.yaml` | `kpi_engine/**` |
| `udfs/config/models/<name>.yaml` only if the source is new | `udfs/kpi_engine/main.py` |
| `tests/` for this KPI | `adapter.py`, `orchestrator.py` |

### 4.2 New source shape (joins or CTE)

**Examples:** extra dimension table, eligibility CTE, SCD in SQL.

| Change | Do not change |
|---|---|
| `udfs/config/models/<name>.yaml` (`kind: physical` or `sql`) | KPI `measures` math (keep catalog ops) |
| KPI `model:` pointer | `core/` (a new measure name is capabilities + registries) |

Put messy SQL in the **model**, not in `measures.sql` (that field is a **column name** only).

Runtime scan paths come from `context.datasets` (`$alias_path` in SQL models). If a dim table is not on the context, set `default_paths:` or `sources.<alias>.default_path` in the model YAML. A context path always overrides the default.

```yaml
required_aliases: [sotif, regions]
default_paths:
  regions: abfss://container@account/dims/regions.parquet
sql: |
  WITH facts AS (SELECT * FROM read_parquet($sotif_path)),
       regions AS (SELECT * FROM read_parquet($regions_path))
  SELECT f.*, r.weight FROM facts f
  INNER JOIN regions r ON f.region = r.region
```

### 4.3 New combination of existing ops

**Examples:** `value_9m` window, `yoy_12m` as growth of two 12m windows, trend on global+region.

| Change | Do not change |
|---|---|
| New keys under `measures:` (`op: window/point/trend/arithmetic/rank/percent_of_total`) | Python |
| `cuts:` / `measures.*.cuts` for which grains get a trend | Catalog kinds list unless you invent a new `op` |

Example — 9 month trailing sum:

```yaml
value_9m:
  of: sotif_value
  op: window
  trailing: { months: 9 }
  inclusive: true
```

No engine change.

### 4.4 New reusable op, hook, or function

**Examples:** a new measure kind, a series hook, a named column/measure function.

Check [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) first — the name may already exist.

A new **name** is not an engine change. Add the body under `capabilities/` and the allowlist key under `registries/`. Then regenerate `registries/CAPABILITIES.md`. KPI YAML can name it immediately (section 4.3).

| Change | Do not change |
|---|---|
| `capabilities/ops/` + `registries/ops.yaml` | `core/`, `contracts.py` |
| `capabilities/hooks/` + `registries/hooks.yaml` (`requires_value` / `extra_keys` if needed) | `importlib` of a path from YAML |
| `capabilities/functions/` + `registries/functions/` (`min_args` if the fn is variadic and not the default 2) | A one-off `if kpi_id == 3004` |
| Optional: a `compute()` test under `tests/` | `udfs/kpi_engine/main.py` |

**Still engine work** (not a new catalog name): a new `agg:`, filter operator, compose template, time format, or a new *common* measure field like `offset`. A new extras allowlist key (`min_args`, `requires_value`, `extra_keys`) is a rare one-line loader change.

### 4.5 Logic the catalog cannot express

**Examples:** cohort survival curve, custom allocation, iterative algorithm.

Same two folders as 4.4: add a hook function and a `hooks.yaml` key. KPI YAML uses `op: hook` with `hook: my_hook`.

Hooks receive **already aggregated / aligned** frames. They must not scan ADLS. Declare `offset:` or `trailing:` on the measure so the planner scans enough history.

If two KPIs need the same hook, promote it to a catalog op (4.4) instead. Full signature and rules: [reference §10](kpi-yaml-reference.md#10-when-you-need-a-custom-function).

### 4.6 Context or platform change

**Examples:** new filter field, multiple views per request, new month filter name convention.

| Change | Do not change |
|---|---|
| `kpi_engine/core/adapter.py` — parse the new envelope | KPI YAML as a workaround for a context bug |
| `tests/test_adapter.py` | `calc_engine` for parsing JSON |

Month filter **name** is per-KPI (`time.filter_code`). A new name for one KPI is YAML only (4.1), not an adapter change.

### 4.7 New UDF module name

Do not add a second entry. Metadata calls `udfs.kpi_engine.main` for every KPI. Routing is `execution.kpi_id` → `config/kpis/<id>.yaml`.

---

## 5. File map (quick reference)

### Authoring (change often)

| File | Change when |
|---|---|
| `udfs/config/kpis/<kpi_id>.yaml` | New or updated KPI definition |
| `udfs/config/models/<model_id>.yaml` | New extract / joins / SQL model |
| `tests/test_*.py` | Prove this KPI or this op |

### Engine (change rarely)

| File | Change when | Do not use for |
|---|---|---|
| `core/adapter.py` | Context JSON shape | KPI formulas |
| `core/binder.py` | A new *common* measure field (shared by every kind) | A new measure name |
| `core/loader.py` | A new extras allowlist key (`min_args`, `requires_value`, `extra_keys`) | A new catalog name |
| `core/op_protocol.py` | The `OpPlugin` façade itself | A new op body |
| `core/time_planner.py` | A new time format / grain | Lookback for one catalog name |
| `core/filters.py` | Filter bind / `ignore_filters` | Hierarchy expansion (upstream) |
| `core/model_sql.py` | DuckDB retrieve (scans, joins, filters, model columns) | KPI YAML formulas |
| `core/cuts.py` | Cut walk / finest grain | Listing G/R in Python |
| `core/calc_engine.py` | Pipeline dispatch (not a new op body) | A new measure kind |
| `core/fn_apply.py` | Function maps and Pandas apply (engine) | New function bodies |
| `core/hook_registry.py` | In-memory hook map (loader fills it) | New hook bodies |
| `core/orchestrator.py` | Pipeline order | Business metrics |
| `capabilities/functions/` + `registries/functions/` | A function every KPI should reuse | Per-KPI formulas |
| `capabilities/ops/` + `registries/ops.yaml` | A new measure kind | Per-KPI formulas |
| `capabilities/hooks/` + `registries/hooks.yaml` | A new named hook | Import paths from context |
| `registries/CAPABILITIES.md` | Regenerated after a registry change (`write_generated_docs()`) | Hand edits |
| `udfs/kpi_engine/main.py` | Never, except the UDF signature | Calculations |
| `contracts.py` | New typed fields for YAML/context | Parsing or SQL |

### Never put in KPI YAML

- Delta / ABFSS paths
- Python / `eval` / `df['col'].sum()`
- DuckDB connection or credentials
- YoY implemented as ad-hoc SQL instead of `op: arithmetic`

---

## 6. Decision tree

```text
Need a new KPI?
  ├─ Same SUM + 3m/12m/YoY/trend/cuts?
  │    → udfs/config/kpis/<id>.yaml only
  ├─ New tables or CTE?
  │    → udfs/config/models/<id>.yaml + KPI YAML
  ├─ New combo of existing catalog names?
  │    → KPI YAML measures only (see CAPABILITIES.md)
  ├─ New named op / function / hook?
  │    → capabilities/ + registries/ + regenerate CAPABILITIES.md; not core/
  ├─ One-off algorithm?
  │    → capabilities/hooks/ + registries/hooks.yaml; not core forks
  └─ Context JSON changed?
       → adapter.py + test_adapter.py
```

---

## 7. Worked examples

### A. New KPI, same Sotif model, add 9m and keep G/R

1. Copy `udfs/config/kpis/3004.yaml` → `udfs/config/kpis/3010.yaml`.
2. Set `kpi_id: 3010`.
3. Add:

```yaml
value_9m:
  of: sotif_value
  op: window
  trailing: { months: 9 }
  inclusive: true
```

4. Ask metadata to send `measure_key: value_9m` when needed.
5. **Files not touched:** entire `kpi_engine/` tree.

### B. Need “same month last year” on a new fact table

1. New `udfs/config/models/orders.yaml` (`required_aliases: [orders]`).
2. New `udfs/config/kpis/4001.yaml` with `model: orders`, `base_measures` on that fact, `measures.previous_year_value` with `op: point`, `offset: { years: 1 }`.
3. **Files not touched:** `calc_engine.py` (point + years already exists).

### C. Need a new op `rolling_median`

1. Add a class under `capabilities/ops/` (`extra_keys` on the class if it has its own YAML fields).
2. Add a row in `registries/ops.yaml`.
3. Regenerate `registries/CAPABILITIES.md`.
4. Optional: a `compute()` test under `tests/`.
5. After that, KPIs only add YAML `op: rolling_median`. No `core/` edit.

---

## 8. Common failures

| Symptom | Likely cause | Fix in |
|---|---|---|
| `Unknown measure_key` | Context key not in YAML `measures:` | KPI YAML |
| `Missing month filter` | `time.filter_code` ≠ context filter key | KPI YAML or metadata |
| Previous year all null | Month applied as IN; or span too short | Must be range (engine); check requested keys widen span |
| Unmapped filter error | No `filter_column_mappings` for that filter | Metadata mappings or YAML `filter_map` |
| `heir` error | Hierarchical filter not expanded | Context builder, not the engine |
| G equals only one region | Region pushed to DuckDB | Cut `ignore_filters`; engine defers that filter |

When in doubt: **YAML first**, catalog op second, hook third, adapter last.
