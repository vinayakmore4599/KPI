# Onboarding a New Hook

**Audience:** Developers, tech leads, architects  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md §20–§21.2, kpi-onboarding-guide.md §4.4–4.5, docs/capability-contract.md, kpi_engine/registries/CAPABILITIES.md

---

## 1. Definition

A **hook** is a registered Python callable that implements an algorithm on an **aligned period time series** for one dimension combo. Hooks are never invoked directly from YAML by import path — KPI authors always reach them through the **`hook` op**: `op: hook` + `hook: <allowlisted_name>`.

Hooks fill the gap between first-class **ops** (fixed measure kinds with bind/lookback contracts) and one-off KPI logic. They are ideal for series algorithms that are reusable but not yet (or not worth) promoting to a full `OpPlugin`.

---

## 2. Purpose and pipeline role

Hooks run in the **combo phase**, after the monthly spine is densified and base measures exist as period-level values for each combo group.

```
DuckDB extract
    → fold + densify monthly spine
    → for each emitted cut:
        for each dimension combo:
            evaluate combo ops
                → Hook op → hook_registry.run(name, series, kpi, plan, spec, …)
    → cut-phase ops
    → JSON rows
```

| Component | Path | Role |
|---|---|---|
| Allowlist | `kpi_engine/registries/hooks.yaml` | Closed catalog of hook names |
| Registry | `kpi_engine/pipeline/hook_registry.py` | `REGISTRY`, `register()`, `run()` |
| Implementations | `kpi_engine/capabilities/hooks/impl.py` | Hook function bodies |
| Bridge op | `kpi_engine/capabilities/ops/combo.py` — class `Hook` | Validates name, plans lookback, calls `run()` |
| Loader | `kpi_engine/pipeline/loader.py` | Imports enabled hooks at startup |

The hook receives **already aggregated, calendar-aligned** data for the combo. It must **not** scan ADLS or open new DuckDB queries.

---

## 3. When to use / when not to use

### Use a hook when

- The logic operates on a **trailing or offset window of period values** for one combo (EWMA, hit rate, seasonal index).
- The algorithm is **niche or experimental** — not yet a first-class measure kind.
- You need **extra YAML keys** on the measure (`value:`, `cohort_column:`, `alpha:`) declared via `extra_keys` in `hooks.yaml`.
- A **dual-key output** is needed (e.g. forecast low/high) — bind expands keys per contract.

### Do not use a hook when

| Need | Use instead |
|---|---|
| Standard trailing sum/average (`window` op) | **Op** — already platform |
| YoY / MoM compare on a base measure | **`compare` sugar** → period ops |
| Rank or % of total across a cut | **Cut-phase op** (`rank`, `percent_of_total`) |
| Row-wise fact math | **Column function** |
| Scalar formula over two measure outputs | **Measure function** |
| Second KPI needs same logic as a **stable kind** | **Promote to op** (see §11) |

---

## 4. Design principles

### 4.1 Scalability — catalog growth without pipeline edits

Hooks are **Tier 3** extensions: add function + `hooks.yaml` row. The `Hook` op class and `hook_registry.run()` are already in the frozen pipeline.

### 4.2 Minimal change

Standard onboarding:

1. `kpi_engine/capabilities/hooks/impl.py` — one function
2. `kpi_engine/registries/hooks.yaml` — one row

Optional: declare `requires_value: true` or `extra_keys` for bind-time validation.

### 4.3 Closed-world allowlist

YAML sets `hook: ewma`, never a Python module path. Unknown names fail at bind.

### 4.4 Lookback is mandatory

Hooks almost always need history. KPI YAML **must** set `trailing:` or `offset:` on the measure so `plan_time()` scans enough months. Missing lookback → wrong or null results.

### 4.5 All hooks are addons

Every hook in the catalog is `role: addon`. They can be `enabled: false` without breaking engine startup. Platform KPIs should not hard-depend on a disabled addon without a fallback.

### 4.6 Promote when mature

If two or more KPIs need the same behavior with stable semantics, **promote hook → op** (e.g. fixed-N compound growth as op vs trailing CAGR as hook).

---

## 5. Comparison with other extension types

| Dimension | Hook | Op | Measure fn | Column fn |
|---|---|---|---|---|
| Registry | `hooks.yaml` | `ops.yaml` | `functions/measure.yaml` | `functions/column.yaml` |
| YAML entry | `op: hook` + `hook:` | `op: window/…` | `op: fn` | `base_measures` + `op:` |
| Body type | Callable | `OpPlugin` class | Callable | Callable |
| Input | Period series DataFrame | `EvalCtx` | Scalars | `pd.Series` |
| Phase | Combo (via Hook op) | Combo or cut | Post-fold scalar | Pre-fold row |
| Lookback | On measure (`trailing`/`offset`) | `OpPlugin.lookback()` | From inputs | N/A |
| Cut-wide logic | No | Yes (`phase: cut`) | No | No |

**Hook vs op overlap:** Some behaviors exist in both shapes by design. Pick **one** — document the choice in the KPI. Ops give stricter bind contracts; hooks allow faster iteration.

---

## 6. Decision guide

```
Algorithm needs aligned period history for one combo?
├─ NO → not a hook (consider measure fn or column fn)
└─ YES
    ├─ Matches existing op (window, lag, pct_change, …)?
    │   └─ Use existing op — no new code
    ├─ Needs rank/share across entire cut?
    │   └─ Cut-phase op
    ├─ Stable reusable measure kind with bind/lookback/trend contract?
    │   └─ New OpPlugin
    └─ Niche series algorithm, addon catalog entry?
        └─ Hook
```

---

## 7. Onboarding guide (step-by-step)

### Step 1 — Check the catalog

Open `kpi_engine/registries/CAPABILITIES.md` (section **Hooks**). 36 hooks ship in the default catalog (`ewma`, `hit_rate`, `cohort_retention`, …).

### Step 2 — Implement the hook function

Add to `kpi_engine/capabilities/hooks/impl.py`:

```python
def ewma(series, *, kpi, plan, spec, detail=None, combo=None, group_dims=None, **_):
    """Exponential weighted moving average; alpha = 2 / (N + 1)."""
    values = series["value"].tolist()
    if not values:
        return None
    alpha = 2.0 / (len(values) + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result
```

**Signature expectations:** The `Hook` op passes keyword context. Accept `**_` for forward compatibility. Do not mutate inputs in place unless documented.

### Step 3 — Register in hooks.yaml

```yaml
ewma:
  role: addon
  enabled: true
  aliases: []
  description: Recency-weighted average of period values. Alpha = 2 / (N + 1).
  example: |
    smoothed:
      op: hook
      hook: ewma
      of: sotif_value
      trailing: { months: 12 }
  module: kpi_engine.capabilities.hooks.impl
  attr: ewma
```

| Key | Required | Notes |
|---|---|---|
| `role` | Yes | Hooks are typically `addon` |
| `enabled` | Yes | |
| `description` / `example` | Yes | |
| `module` / `attr` | Yes | |
| `requires_value` | No | If measure must include `value:` (threshold bar) |
| `extra_keys` | No | Additional YAML keys validated at bind (e.g. `cohort_column`) |

### Step 4 — Regenerate catalog

```python
from kpi_engine.pipeline.loader import write_generated_docs
write_generated_docs()
```

### Step 5 — Use in KPI YAML

```yaml
measures:
  smoothed:
    op: hook
    hook: ewma
    of: sotif_value
    trailing: { months: 12 }
```

**With threshold (`requires_value: true`):**

```yaml
measures:
  months_on_sla:
    op: hook
    hook: hit_rate
    of: sotif_value
    trailing: { months: 12 }
    value: 95
```

**With extra keys (cohort):**

```yaml
measures:
  retain:
    op: hook
    hook: cohort_retention
    of: sotif_value
    trailing: { months: 12 }
    cohort_column: supplier_name
    entry_period: "2025-01-01"
```

### Step 6 — Test

Add tests under `tests/test_hooks_advanced.py` or similar:

1. Bind — unknown hook rejected; missing `value:` rejected when `requires_value`
2. Lookback — planner includes enough months (`tests/test_lookback_planning.py`)
3. Compute — parquet fixture with expected scalar

### Do not change

| File | Reason |
|---|---|
| `hook_registry.py` | Generic `run()` already exists |
| `capabilities/ops/combo.py` `Hook` class | Unless adding new shared hook op fields (Tier 4) |
| `pipeline/orchestrator.py` | No per-hook retrieve logic |

---

## 8. Worked examples

### Example 1 — EWMA smoothing

```yaml
measures:
  sotif_value:
    op: point
    of: sotif_fact
  smoothed_12m:
    op: hook
    hook: ewma
    of: sotif_value
    trailing: { months: 12 }
```

Returns one scalar at the anchor month — weighted toward recent periods in the 12-month window.

### Example 2 — Hit rate vs SLA threshold

Hook `hit_rate` counts periods where `of` measure ≥ `value:`.

```yaml
measures:
  months_on_sla:
    op: hook
    hook: hit_rate
    of: sotif_value
    trailing: { months: 12 }
    value: 95
```

### Example 3 — Seasonal index

```yaml
measures:
  seasonal:
    op: hook
    hook: seasonal_index
    of: sotif_value
    trailing: { months: 36 }
```

Compares anchor month to historical same-calendar-month average.

---

## 9. Testing checklist

- [ ] Hook name in `REGISTRY` after import
- [ ] `validate()` / bind succeeds with example YAML
- [ ] Missing required `value:` fails at bind (if `requires_value`)
- [ ] Unknown `extra_keys` rejected at bind
- [ ] Lookback spans enough months for trailing window
- [ ] `compute()` result matches hand-calculated oracle on fixture
- [ ] Hook does not perform I/O (no ADLS / DuckDB)

---

## 10. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| Omitting `trailing:` / `offset:` | Always declare lookback on hook measures |
| Scanning raw parquet inside hook | Use series passed by engine only |
| Adding hook for simple 3-month sum | Use `op: window` |
| Import path in YAML | Use allowlisted `hook:` name only |
| Keeping hook when 5+ KPIs need identical kind semantics | Promote to `OpPlugin` |

---

## 11. Promoting a hook to an op

When a hook becomes a first-class measure kind:

1. Create `OpPlugin` subclass in `capabilities/ops/combo.py` (or `period.py`).
2. Register in `ops.yaml` with `description`, `example`, `extra_keys`.
3. Migrate KPI YAML from `op: hook` to `op: new_kind`.
4. Deprecate hook entry (`enabled: false`) after migration.

Benefits: explicit `lookback()` / `validate()` / `shiftable` flags; clearer CAPABILITIES documentation; better bind errors.

---

## 12. When this is not enough (Tier 4)

Pipeline changes needed when:

- Hook op must pass **new context** not in current `Hook.evaluate()` contract
- Hook needs **cut-phase** visibility (use cut op instead)
- New **global** measure field for all ops/hooks

---

## Regenerate Word document

```bash
pip install -e ".[docs]"
python scripts/generate_onboarding_docs.py
```

Output: `docs/onboarding/onboarding-hook.docx`
