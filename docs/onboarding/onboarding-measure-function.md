# Onboarding a New Measure Function

**Audience:** Developers, tech leads, architects  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md §20–§21.4, kpi-onboarding-guide.md §4.4, kpi-yaml-reference.md §10.2, kpi_engine/registries/CAPABILITIES.md

---

## 1. Definition

A **measure function** is a registered scalar transform that takes one or more **already-aggregated measure values** (numbers or nulls produced by base measures, points, windows, or other ops) and returns a single scalar. Measure functions run **after** the `agg:` fold and combo-phase evaluation — they never see raw fact rows.

Measure functions are the right extension when KPI authors need reusable formulas over computed measures: growth percentage, safe ratio, clamping, attainment vs target, or chained arithmetic across measure keys.

---

## 2. Purpose and pipeline role

Measure functions sit in the **post-fold calculation layer**. Base measures are folded first; then ops like `point`, `window`, and `trend` produce scalars or series; measure functions combine those scalars.

```
DuckDB extract
    → apply_pandas_facts (column fns + agg)
    → densify monthly spine
    → for each cut / combo:
        evaluate combo ops (point, window, trend, fn, hook, …)
            → call_measure_fn() when op is fn / arithmetic / expr
    → cut-phase ops (rank, percent_of_total)
    → cut-derived fn / arithmetic
    → JSON rows
```

| Stage | What measure functions do |
|---|---|
| Bind | Combo ops `Fn`, `Arithmetic`, `Expr` validate `fn` ∈ `MEASURE_FNS` and `inputs:` arity |
| Combo eval | `call_measure_fn()` applies the registered callable to resolved input scalars |
| Trend slots | `TrendArithmetic` can apply measure fns per period |
| Cut-derived | `_apply_cut_derived()` in `calc_engine.py` for post-cut scalar math |

**Key files:**

| Role | Path |
|---|---|
| Runtime map | `kpi_engine/pipeline/fn_apply.py` — `MEASURE_FNS`, `call_measure_fn()` |
| Loader | `kpi_engine/pipeline/loader.py` — reads `functions/measure.yaml` |
| Allowlist | `kpi_engine/registries/functions/measure.yaml` |
| Implementations | `kpi_engine/capabilities/functions/measure/impl.py` |
| Combo dispatch | `kpi_engine/capabilities/ops/combo.py` — `Fn`, `Arithmetic`, `Expr` |
| Cut-derived | `kpi_engine/pipeline/calc_engine.py` — `_apply_cut_derived()` |

---

## 3. When to use / when not to use

### Use a measure function when

- The formula combines **outputs of other measures** (not physical columns).
- Multiple KPIs need the same scalar math (`growth_pct`, `divide`, `clamp`).
- Authors use `op: fn` + `inputs:`, `op: arithmetic` + `fn:`, or `op: expr` calling registered names.
- Input order matters — prefer `op: fn` + named `inputs: { current: …, previous: … }` over legacy `arithmetic`.

### Do not use a measure function when

| Need | Use instead |
|---|---|
| Row-wise math on physical columns before `agg:` | **Column function** |
| Smoothing / stats over a **time series** (EWMA, rolling MAD) | **Hook** or **period op** |
| New behavior needing **lookback rules**, trend arrays, or cut partitioning | **Op** (`OpPlugin`) |
| Single KPI one-liner with no reuse | Existing `growth_pct` / `divide` in catalog first |

---

## 4. Design principles

### 4.1 Scalability — freeze the pipeline, grow the catalog

Adding a measure function is **Tier 3** onboarding: body + registry row only. The pipeline dispatch (`call_measure_fn`, combo ops) already exists; you add a new name to the map.

### 4.2 Minimal change

Standard onboarding touches **two files**:

1. `kpi_engine/capabilities/functions/measure/impl.py`
2. `kpi_engine/registries/functions/measure.yaml`

Regenerate `CAPABILITIES.md`. No changes to `calc_engine.py` for a typical new scalar formula.

### 4.3 Closed-world allowlist

Unknown `fn:` names fail at bind. YAML never imports Python modules by path.

### 4.4 Scalar contract

- **Input:** Python scalars (typically `int`, `float`, or `None`) — one per `inputs:` entry.
- **Output:** Scalar or `None` (undefined).
- **Null propagation:** Any null input → null result (platform convention).
- **Divide-by-zero:** Returns null, never raises in production eval.

### 4.5 Binary fold for variadic inputs

Two-argument functions given **three or more** inputs fold left-to-right (legacy `arithmetic` behavior). Document this if your function is variadic.

### 4.6 Same name, different registry as column fn

`divide` exists in both `column.yaml` and `measure.yaml` as **different callables** (`divide_columns` vs `divide_scalars`). This is intentional — same author-facing name, different pipeline stage.

---

## 5. Comparison with other extension types

| Dimension | Measure fn | Column fn | Hook | Op |
|---|---|---|---|---|
| Registry | `functions/measure.yaml` | `functions/column.yaml` | `hooks.yaml` | `ops.yaml` |
| When | After aggregation | Before aggregation | Combo on series | Combo or cut lifecycle |
| Input | Scalars | `pd.Series` | Aligned period frame | Child measures via `EvalCtx` |
| YAML | `op: fn` / `arithmetic` / `expr` | `base_measures` + `op:` | `op: hook` | `op: window/rank/…` |
| Extends via | Callable | Callable | Callable | `OpPlugin` class |
| Lookback | Inherited from input measures | N/A | On hook measure | `OpPlugin.lookback()` |

**Rule of thumb:** if both operands are **measure keys** in `measures:`, use a measure function. If operands are **physical columns** in the extract, use a column function.

---

## 6. Decision guide

```
Formula inputs are…
├─ Physical fact columns (before any agg)?
│   └─ Column function
├─ Other measure_key values (scalars at this anchor/cut)?
│   └─ Measure function
├─ Full aligned monthly series for one combo?
│   ├─ Needs reusable measure kind semantics?
│   │   └─ Op
│   └─ Experimental / niche series algorithm?
│       └─ Hook
└─ Must rank or share across all groups on a cut?
    └─ Cut-phase Op
```

---

## 7. Onboarding guide (step-by-step)

### Step 1 — Check the catalog

Open `kpi_engine/registries/CAPABILITIES.md` (section **Measure functions**). Common names already exist: `growth_pct`, `divide`, `percent`, `subtract`, `clamp`, `attainment`.

### Step 2 — Implement the function

Add to `kpi_engine/capabilities/functions/measure/impl.py`:

```python
def safe_ratio_scalars(numerator, denominator):
    """Ratio with null on zero or null denominator."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator
```

### Step 3 — Register in the allowlist

```yaml
safe_ratio:
  role: addon
  enabled: true
  aliases: []
  description: Ratio of two scalars; null when denominator is zero or null.
  example: |
    otd_pct:
      op: fn
      fn: safe_ratio
      inputs: [ontime_value, total_value]
  module: kpi_engine.capabilities.functions.measure.impl
  attr: safe_ratio_scalars
```

| Key | Required | Notes |
|---|---|---|
| `role` | Yes | `platform` or `addon` |
| `enabled` | Yes | |
| `description` | Yes | |
| `example` | Yes | Valid `measures:` fragment |
| `module` / `attr` | Yes | |
| `aliases` | No | |
| `min_args` | No | For `*args` variadic functions |

### Step 4 — Regenerate catalog

```python
from kpi_engine.pipeline.loader import write_generated_docs
write_generated_docs()
```

### Step 5 — Use in KPI YAML

**Explicit fn op (preferred when argument order matters):**

```yaml
measures:
  yoy:
    op: fn
    fn: growth_pct
    inputs: [current_value, previous_year_value]
```

**Named inputs:**

```yaml
measures:
  yoy_growth:
    op: fn
    fn: growth_pct
    inputs: { previous: previous_year_value, current: current_value }
```

**Arithmetic shorthand:**

```yaml
measures:
  rate:
    op: arithmetic
    fn: divide
    left: shipped_value
    right: ordered_value
```

**Expression:**

```yaml
measures:
  capped:
    op: expr
    expr: "clamp(current_value, floor, cap)"
```

### Step 6 — Test

Reference tests: `tests/test_function_catalog.py`, `tests/test_measure_fn_extended.py`, `tests/test_addon_ops.py`.

### Do not change

| File | Reason |
|---|---|
| `kpi_engine/pipeline/calc_engine.py` | Dispatch already generic |
| `capabilities/ops/combo.py` | Unless adding new op kind (not new fn) |
| Per-KPI Python | Use YAML + catalog |

---

## 8. Worked examples

### Example 1 — YoY growth (`growth_pct`)

```yaml
base_measures:
  sotif_value:
    sql: sotif_value
    agg: sum

measures:
  current_value:
    op: point
    of: sotif_value
  previous_year_value:
    op: point
    of: sotif_value
    offset: { years: 1 }
  yoy:
    op: fn
    fn: growth_pct
    inputs: [current_value, previous_year_value]
```

If current = 110 and previous = 100 → `10.0` (% change).

### Example 2 — Target attainment chain

From `tests/test_addon_ops.py`:

```yaml
measures:
  gap:
    op: fn
    fn: subtract
    inputs: [current_value, target]
  magnitude:
    op: fn
    fn: abs
    inputs: [gap]
  vs_goal:
    op: fn
    fn: attainment
    inputs: [current_value, target]
```

Each step is a separate measure key the UI can request independently.

### Example 3 — Safe ratio for OTD percentage

```yaml
measures:
  ontime_value:
    op: point
    of: ontime_qty
  total_value:
    op: point
    of: total_qty
  otd_pct:
    op: fn
    fn: safe_ratio
    inputs: [ontime_value, total_value]
```

Null when `total_value` is 0 — avoids divide-by-zero in dashboards.

---

## 9. Testing checklist

- [ ] Registered in `MEASURE_FNS` after engine import
- [ ] Bind rejects unknown `fn:` with helpful message
- [ ] Bind rejects wrong `inputs:` count
- [ ] Null inputs → null output
- [ ] Zero denominator → null (for ratio fns)
- [ ] `compute()` on parquet matches oracle
- [ ] Named `inputs: { param: measure }` wiring works

---

## 10. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| Using measure fn for `ontime * fullqty` on fact rows | Column fn + `agg: sum` |
| Swapping growth_pct argument order without named inputs | Use `inputs: { current: …, previous: … }` |
| Adding logic in `call_measure_fn` for one KPI | Add function to `impl.py` + registry |
| Expecting measure fn to plan lookback | Lookback comes from input measures (`point`, `window`) |
| Using `op: fn` without listing all input measure keys | Every input must be a key under `measures:` |

---

## 11. When this is not enough (Tier 4)

Escalate to pipeline work when you need:

- A new **`op:` kind** (not just a new `fn:` name)
- New **shared measure YAML fields** parsed by all ops
- Changes to **binary fold** semantics globally
- **Cut-phase** behavior that cannot be expressed as cut-derived `fn`

---

## Regenerate Word document

```bash
pip install -e ".[docs]"
python scripts/generate_onboarding_docs.py
```

Output: `docs/onboarding/onboarding-measure-function.docx`
