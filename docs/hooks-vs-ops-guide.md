# Hooks vs Ops — Conceptual Guide

**Audience:** Developers, tech leads, KPI authors  
**Version:** August 2026  
**Related docs:** docs/onboarding/onboarding-hook.md, docs/onboarding/onboarding-op.md, kpi_engine/registries/CAPABILITIES.md

---

## 1. The short answer

In the KPI engine, **ops** are the main measure types (`point`, `window`, `trend`, `rank`, …). A **hook** is a reusable Python algorithm for a time series — but you never call it directly. You always reach it through the **`hook` op**:

```yaml
op: hook
hook: ewma   # which algorithm to run
```

**Every hook is used via an op**, but **not every op is a hook**.

---

## 2. What is an op?

An **op** is a first-class **measure kind** — a built-in calculation type with a fixed YAML contract.

| Op | What it does |
|---|---|
| `point` | Value at one month (with optional offset) |
| `window` | Sum/average over trailing months |
| `trend` | Array of values for charts |
| `arithmetic` / `fn` | Combine other measures (e.g. YoY %) |
| `rank` | Rank rows within a cut |
| `hook` | Run a named custom algorithm |

When YAML authors write measures, they pick an op:

```yaml
measures:
  current_value:
    op: point
    of: sotif_value

  value_12m:
    op: window
    of: sotif_value
    trailing: { months: 12 }
```

### Real example from SOTIF KPI 3004

```yaml
measures:
  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }

  previous_year_value:
    of: sotif_value
    op: point
    offset: { years: 1 }

  value_3m:
    of: sotif_value
    op: window
    trailing: { months: 3 }
    inclusive: true

  yoy_month:
    op: arithmetic
    fn: growth_pct
    left: current_value
    right: previous_year_value

  trend_12m:
    of: sotif_value
    op: trend
    trailing: { months: 12 }
    inclusive: true
    cuts: [G]
```

### Pipeline phases

Ops run in two phases after extract and densify:

```
Extract data → densify monthly spine
    → COMBO phase (per supplier/region combo): point, window, trend, hook, fn…
    → CUT phase (across whole cut): rank, percent_of_total…
    → JSON output
```

---

## 3. What is a hook?

A **hook** is a **Python function** that runs on an **aligned monthly time series** for one dimension combo (e.g. one supplier in one region).

The engine has already:

1. Pulled data from DuckDB
2. Aggregated by month
3. Built a dense spine of period values

The hook receives that series and returns **one number** (or a low/high pair for forecasts).

### When to use a hook

Use a hook when the catalog does not have a standard op for your algorithm:

- EWMA smoothing
- Hit rate (% of months above a threshold)
- Seasonal index (this month vs same month in prior years)
- Cohort retention

### YAML shape

```yaml
measures:
  smoothed_12m:
    op: hook          # the op (bridge)
    hook: ewma        # which hook algorithm
    of: sotif_value   # input measure
    trailing: { months: 12 }   # MUST set lookback
```

Without `trailing:` or `offset:`, the hook may not get enough history and can return wrong or null results.

---

## 4. How they connect

```
KPI YAML
   op: hook  +  hook: ewma
        │
        ▼
   Hook op (OpPlugin in combo.py)   ← this IS an op
        │
        ▼
   hook_registry.run("ewma", series, …)
        │
        ▼
   ewma() function in hooks/impl.py  ← this IS the hook
```

- **`op: hook`** = platform op (always available)
- **`hook: ewma`** = which allowlisted function from hooks.yaml to call

Current catalog: **58 ops** and **36 hooks**.

---

## 5. Side-by-side examples

Same starting point: “something about the last 12 months of SOTIF value.”

### Standard op — 12-month total

```yaml
value_12m:
  op: window
  of: sotif_value
  trailing: { months: 12 }
  inclusive: true
```

**Result:** Simple sum over 12 months. Fixed semantics, well-defined bind/lookback.

### Hook — exponentially weighted average

```yaml
smoothed_12m:
  op: hook
  hook: ewma
  of: sotif_value
  trailing: { months: 12 }
```

**Result:** Custom math — recent months weighted more than older ones. Not expressible as `window`.

### Hook — SLA hit rate

```yaml
months_on_sla:
  op: hook
  hook: hit_rate
  of: sotif_value
  trailing: { months: 12 }
  value: 95          # threshold — hook-specific extra key
```

**Result:** Counts how many months were ≥ 95%. Needs `value:` — a hook-specific parameter.

### Standard op — YoY growth (not a hook)

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value
  right: previous_year_value
```

**Result:** Combines two other measures. No time-series algorithm needed.

---

## 6. When to use which?

```
Need a new measure?
│
├─ Standard time behavior? (sum, point, lag, rank, trend)
│     → Use an existing OP (window, point, lag, rank, …)
│
├─ Combine outputs of other measures? (ratio, YoY %)
│     → op: fn or op: arithmetic
│
├─ Custom algorithm on monthly history for one combo?
│     → op: hook + hook: <name>
│
└─ Same custom logic needed by many KPIs with stable rules?
      → Promote hook → new OP (e.g. compound_growth op vs cagr hook)
```

| Situation | Use |
|---|---|
| 3/6/12 month totals | `op: window` |
| Value last year same month | `op: point` + `offset` |
| Chart series | `op: trend` |
| Rank suppliers in a cut | `op: rank` (cut phase) |
| EWMA, seasonal index, hit rate | `op: hook` |
| `(A - B) / A` from two measures | `op: arithmetic` or `op: fn` |

---

## 7. Key differences

| Dimension | Op | Hook |
|---|---|---|
| What it is | Measure kind (`OpPlugin` class) | Python function on a time series |
| YAML | `op: window` | `op: hook` + `hook: ewma` |
| Registry | ops.yaml (~58 kinds) | hooks.yaml (~36 algorithms) |
| Contract | Strict: bind, lookback, deps, phase | Looser; good for experimentation |
| Examples | point, window, trend, rank, lag | ewma, hit_rate, seasonal_index, cagr |
| Cut-wide logic | Yes (`rank`, `percent_of_total`) | No — combo only |
| Maturity | Platform / first-class | Addon / niche; promote when stable |

---

## 8. Mental model

- **Ops** = the **vocabulary** of the KPI engine — the words you compose measures from.
- **Hooks** = **custom plugins** for series math that is not worth (yet) making a full op.
- The **`hook` op** is the door — it is the only way YAML reaches hook code.

---

## 9. Rule of thumb

1. **Can I do this with `window`, `point`, `lag`, or `fn`?** → use an **op**.
2. **Do I need special series math (EWMA, seasonal, cohort, forecast bands)?** → use **`op: hook`**.
3. **Do five KPIs need the same hook with identical rules?** → promote it to a **new op**.

---

## 10. Further reading

| Topic | Document |
|---|---|
| Onboarding a new hook | docs/onboarding/onboarding-hook.md |
| Onboarding a new op | docs/onboarding/onboarding-op.md |
| Full capability catalog | kpi_engine/registries/CAPABILITIES.md |
| Bind and output contracts | docs/capability-contract.md |
