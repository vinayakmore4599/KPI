# Onboarding a New Column Function

**Audience:** Developers, tech leads, architects  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md §20–§21.3, kpi-onboarding-guide.md §4.4, kpi-yaml-reference.md §10.1, kpi_engine/registries/CAPABILITIES.md

---

## 1. Definition

A **column function** is a registered, row-wise transform that takes one or more Pandas `Series` (physical columns from the DuckDB extract) and returns a `Series` of the same length. Column functions run **before** the `agg:` fold — they operate on every retrieved row, not on aggregated measure values.

Column functions are the right extension when KPI authors need reusable math across physical columns at fact-row granularity: multiply two quantity columns, divide shipped by ordered, trim a string dimension, or apply conditional logic row-by-row.

---

## 2. Purpose and pipeline role

The KPI engine separates **extract** (DuckDB) from **calculation** (Pandas). Column functions belong to the **pre-fold fact layer**: they turn raw columns into named base measures that `agg:` then folds.

```
Host context
    → bind KPI YAML
    → DuckDB retrieve (GROUP BY at extract grain)
    → fold_extract_columns
    → apply_dimension_maps
    → apply_pandas_facts          ← column functions run here
    → collapse_pandas_detail (agg: sum/avg/…)
    → densify monthly spine
    → combo/cut ops (point, window, rank, …)
    → JSON rows
```

| Stage | What column functions do |
|---|---|
| Bind | `binder.py` checks `op` is in `COLUMN_FNS`; validates arity and `expr:` call names |
| Retrieve | DuckDB returns grouped rows; no column-fn logic in SQL |
| Fact apply | `apply_row_op()` or `eval_expr_series()` evaluates each base measure |
| Agg | `agg: sum` (etc.) folds the per-row Series into one number per group/month |

**Key files:**

| Role | Path |
|---|---|
| Runtime map | `kpi_engine/pipeline/fn_apply.py` — `COLUMN_FNS`, `apply_row_op()`, `apply_pandas_facts()` |
| Loader | `kpi_engine/pipeline/loader.py` — reads `functions/column.yaml` at startup |
| Allowlist | `kpi_engine/registries/functions/column.yaml` |
| Implementations | `kpi_engine/capabilities/functions/column/impl.py` |
| Bind validation | `kpi_engine/pipeline/binder.py` — `_parse_base_measure()` |

---

## 3. When to use / when not to use

### Use a column function when

- The math combines **physical columns on the same fact row** before aggregation.
- Multiple KPIs will reuse the same row-wise formula (e.g. `ontime * fullqty`).
- Authors want `columns: [a, b]` + `op: my_fn` or `expr: "my_fn(a, b)"` in `base_measures:`.
- The result is still row-level data that needs `agg: sum`, `agg: avg`, etc.

### Do not use a column function when

| Need | Use instead |
|---|---|
| Math over **already-computed measure scalars** (YoY %, ratio of two KPI outputs) | **Measure function** (`op: fn`) |
| Algorithm on an **aligned time series** for one combo (EWMA, CAGR) | **Hook** (`op: hook`) |
| New **measure kind** with lookback, trend arrays, or cut-wide semantics | **Op** (`OpPlugin` in `capabilities/ops/`) |
| One-off KPI logic that never repeats | Compose existing column fns in KPI YAML first |
| SQL-side transform only this model needs | Model YAML (`kind: sql`) CTE |

---

## 4. Design principles

### 4.1 Scalability — freeze the pipeline, grow the catalog

New column function names are added under **`capabilities/` + `registries/`** only. The frozen pipeline (`kpi_engine/pipeline/`) is not edited for a new function name. This is **Tier 3** onboarding in the architecture scale (see kpi-system-architecture.md §20).

| Scale | What you add | Pipeline change? |
|---|---|---|
| New KPI using existing `multiply` | KPI YAML only | No |
| New column function `weighted_score` | `impl.py` + `column.yaml` row | No |
| New aggregation kind | `pipeline/` + contracts | Yes (Tier 4) |

### 4.2 Minimal change

Touch **two files** for a normal new function:

1. `kpi_engine/capabilities/functions/column/impl.py` — one function body
2. `kpi_engine/registries/functions/column.yaml` — one allowlist row

Then regenerate `CAPABILITIES.md`. Do **not** add `if kpi_id == …` branches, per-KPI Python modules, or import paths in YAML.

### 4.3 Closed-world allowlist

A name is callable only if listed in `column.yaml`. Unknown `op` values fail at **bind time** with the list of registered names. YAML never references Python module paths directly.

### 4.4 Platform vs addon

| Role | Meaning |
|---|---|
| `platform` | Required for core KPIs; must load; cannot be disabled |
| `addon` | Optional catalog entry; `enabled: false` skips registration without breaking startup |

### 4.5 Implementation contract

- **Input:** `pd.Series` arguments, same index/length as the extract frame.
- **Output:** `pd.Series` of equal length.
- **Null safety:** Follow platform conventions — divide-by-zero → null; propagate nulls where appropriate.
- **String ops:** Functions like `trim`, `concat` are in `STRING_COLUMN_OPS`; numeric coercion is skipped for them.

---

## 5. Comparison with other extension types

| Dimension | Column fn | Measure fn | Hook | Op |
|---|---|---|---|---|
| Registry | `functions/column.yaml` | `functions/measure.yaml` | `hooks.yaml` | `ops.yaml` |
| Runtime map | `COLUMN_FNS` | `MEASURE_FNS` | `hook_registry.REGISTRY` | `op_registry.OP_KINDS` |
| Body | Plain callable | Plain callable | Plain callable | `OpPlugin` class |
| Pipeline stage | Pre-fold (fact rows) | Post-fold (scalars) | Combo phase (series) | Combo or cut phase |
| Input type | `pd.Series` per column | Scalars from measures | Aligned period DataFrame | `EvalCtx` + child measures |
| YAML surface | `base_measures` + `columns:` + `op:` | `measures` + `op: fn` | `op: hook` + `hook:` | `measures` + `op: point/window/…` |
| Lookback planning | No (extract span only) | No | Yes (`trailing`/`offset` on hook measure) | Yes (`OpPlugin.lookback`) |

**Rule of thumb:** row math before sum → column fn; scalar math after sum → measure fn; series algorithm → hook or op; new reusable measure **kind** → op.

---

## 6. Decision guide

```
Need reusable logic?
├─ Operates on raw fact columns before agg?
│   └─ YES → Column function
├─ Operates on computed measure values (scalars)?
│   └─ YES → Measure function
├─ Operates on aligned monthly series for one combo?
│   ├─ Should be a first-class measure kind (bind/lookback/trend)?
│   │   └─ YES → Op
│   └─ One-off or experimental series algorithm?
│       └─ YES → Hook
└─ Needs full cut partition (rank all regions)?
    └─ YES → Cut-phase Op
```

---

## 7. Onboarding guide (step-by-step)

### Step 1 — Check the catalog

Open `kpi_engine/registries/CAPABILITIES.md` (section **Column functions**). The name may already exist (`multiply` aliases: `mul`, `product`).

### Step 2 — Implement the function

Add to `kpi_engine/capabilities/functions/column/impl.py`:

```python
def weighted_score_columns(hits: pd.Series, weight: pd.Series) -> pd.Series:
    """Row-wise hits * weight * 10."""
    return hits * weight * 10
```

Requirements:

- Parameters are `pd.Series`; return type is `pd.Series`.
- Same length as the extract frame.
- Use named parameters when authors will wire `columns: { hits: ontime, weight: fullqty }`.

### Step 3 — Register in the allowlist

Add a row to `kpi_engine/registries/functions/column.yaml`:

```yaml
weighted_score:
  role: addon
  enabled: true
  aliases: []
  min_args: 2
  description: Row-wise hits times weight times ten.
  example: |
    sotif_value:
      columns: [ontime, fullqty]
      op: weighted_score
      agg: sum
  module: kpi_engine.capabilities.functions.column.impl
  attr: weighted_score_columns
```

| Key | Required | Notes |
|---|---|---|
| `role` | Yes | `platform` or `addon` |
| `enabled` | Yes | `false` skips registration |
| `description` | Yes | One sentence for CAPABILITIES.md |
| `example` | Yes | Valid `base_measures` fragment |
| `module` | Yes | Must start with `kpi_engine.capabilities.` |
| `attr` | Yes | Python function name |
| `aliases` | No | Alternate YAML names |
| `min_args` | No | For variadic `*columns` functions (default arity from signature) |

### Step 4 — Regenerate the catalog

```python
from kpi_engine.pipeline.loader import write_generated_docs
write_generated_docs()
```

### Step 5 — Use in KPI YAML

**Positional columns:**

```yaml
base_measures:
  ontime_full:
    columns: [ontime, fullqty]
    op: multiply
    agg: sum
```

**Named columns:**

```yaml
base_measures:
  ship_rate:
    columns: { numerator: shipped, denominator: ordered }
    op: divide
    agg: avg
```

**Expression surface (same registry):**

```yaml
base_measures:
  flagged:
    expr: "flag_in_set(region, 'NA', 'EU')"
    agg: sum
```

### Step 6 — Test

Add or extend a test under `tests/`:

1. **Bind** — unknown op rejected; wrong arity rejected.
2. **Compute** — parquet fixture; assert expected cell value.

Reference: `tests/test_function_catalog.py`, `tests/test_column_fn_extended.py`.

### Do not change

| File | Reason |
|---|---|
| `kpi_engine/pipeline/` | Tier 4 — not needed for a new catalog name |
| `kpi_engine/main.py` | Routing is by `kpi_id`, not function name |
| `contracts.py` | Unless adding a new shared YAML field (Tier 4) |
| KPI YAML with hard-coded import paths | Closed-world allowlist only |

---

## 8. Worked examples

### Example 1 — Built-in multiply (row product, then sum)

**Registry:** `multiply` in `column.yaml`  
**Implementation:** `multiply_columns(*columns)` in `impl.py`

```yaml
base_measures:
  ontime_full:
    columns: [ontime, fullqty]
    op: multiply
    agg: sum
```

If row 1 has `ontime=2`, `fullqty=3` → row contribution `6`. With `agg: sum` across rows → total of row products (not product of sums).

### Example 2 — Divide with null on zero denominator

```yaml
base_measures:
  fill_rate:
    columns: { numerator: shipped, denominator: ordered }
    op: divide
    agg: avg
```

`divide_columns` returns null when `ordered == 0` for that row.

### Example 3 — Custom function (test pattern)

From `tests/test_function_catalog.py`:

```python
register_column_fn("weighted_score", lambda hits, weight: hits * weight * 10, min_columns=2)
```

```yaml
base_measures:
  sotif_value:
    columns: [ontime, fullqty]
    op: weighted_score
    agg: sum
```

Two rows `(2,3)` and `(1,4)` → `60 + 40 = 100`.

---

## 9. Testing checklist

- [ ] Name appears in `CAPABILITIES.md` after `write_generated_docs()`
- [ ] `validate(context)` succeeds with KPI using the new `op`
- [ ] Wrong column count fails at bind with clear error
- [ ] `compute(context)` on local parquet matches hand-calculated oracle
- [ ] Null / divide-by-zero behavior documented and tested
- [ ] If variadic: `min_args` set in YAML and tested with 2+ columns

---

## 10. Common mistakes and anti-patterns

| Mistake | Why it is wrong | Fix |
|---|---|---|
| Putting YoY % logic in a column fn | YoY needs two time periods on a measure | Use `op: compare` or measure fn on point measures |
| Editing `fn_apply.py` for one new name | Breaks catalog freeze | Add to `impl.py` + `column.yaml` only |
| Same name, different semantics as measure `divide` | Column and measure registries are separate maps | Document behavior; both can exist with different callables |
| Forgetting `agg:` | Column fn output is still row-level | Always pair with `agg: sum/avg/…` |
| SQL string in `columns:` | `columns:` lists physical column names | Use `sql:` for single column passthrough |

---

## 11. When this is not enough (Tier 4)

Escalate to a **pipeline change** when you need:

- A new **`agg:`** kind (e.g. a new non-additive fold rule)
- A new **filter operator** or time format
- A new **common base_measure field** understood by binder globally
- Row pipeline features (`lookup`, `over`, `expr` grammar extension)

These are architecture changes, not column-function onboarding. Discuss with platform owners before editing `kpi_engine/pipeline/`.

---

## Regenerate Word document

```bash
pip install -e ".[docs]"
python scripts/generate_onboarding_docs.py
```

Output: `docs/onboarding/onboarding-column-function.docx`
