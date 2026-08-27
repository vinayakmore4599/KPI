# Onboarding a New Op

**Audience:** Developers, tech leads, architects  
**Version:** August 2026  
**Related docs:** kpi-system-architecture.md §20–§21.1, kpi-framework-plan.md §7, docs/capability-contract.md, kpi_engine/registries/CAPABILITIES.md

---

## 1. Definition

An **op** (operation / measure kind) is a first-class calculation type registered as an **`OpPlugin`** class. Each op defines how a slice of KPI YAML binds, plans lookback, declares dependencies, and evaluates at **combo phase** (per dimension group) and/or **cut phase** (across all groups on a cut).

Ops are the primary extension for **new measure behavior**: time selection (`point`), trailing windows (`window`), trend arrays (`trend`), calendar shifts (`lag`, `pct_change`), cut ranking (`rank`), share of total (`percent_of_total`), and composition wrappers (`fn`, `hook`, `expr`).

YAML authors set `measures.<key>.op: <kind>`. The engine resolves `<kind>` through `registries/ops.yaml` → `op_registry.OP_KINDS`.

---

## 2. Purpose and pipeline role

Ops orchestrate the **calculation layer** after extract and densify. They are the only extension type with a full **lifecycle**: parse → validate → dependencies → lookback → evaluate.

```
bind (require_op → plugin.parse/validate)
    → plan_time (plugin.lookback per requested measure)
    → extract + densify
    → compute_cuts:
        for each cut:
            for each combo:
                COMBO PHASE: evaluate() → get_op(kind).evaluate(EvalCtx)
            CUT PHASE: apply_to_cut() for phase="cut" ops
            CUT-DERIVED: fn/arithmetic on cut outputs
    → JSON rows
```

**Locked composition order** (from kpi-framework-plan.md):

1. DuckDB extract + base aggregation  
2. Densify monthly spine  
3. **Combo phase** — point, window, trend, arithmetic, fn, expr, hook, period ops  
4. **Cut phase** — rank, percent_of_total, ntile, …  
5. **Cut-derived** — arithmetic/fn/expr consuming cut-phase outputs  

**Key files:**

| Role | Path |
|---|---|
| Allowlist | `kpi_engine/registries/ops.yaml` (~58 ops) |
| Registry | `kpi_engine/pipeline/op_registry.py` |
| Protocol | `kpi_engine/pipeline/op_protocol.py` — `OpPlugin`, `EvalCtx` |
| Combo ops | `kpi_engine/capabilities/ops/combo.py` |
| Period ops | `kpi_engine/capabilities/ops/period.py` |
| Cut ops | `kpi_engine/capabilities/ops/cut.py` |
| Dispatch | `kpi_engine/pipeline/calc_engine.py` — `evaluate()`, `apply_to_cut()` |
| Bind | `kpi_engine/pipeline/binder.py` — `require_op()`, desugar (`compare`, `filtered_*`) |

---

## 3. When to use / when not to use

### Use a new op when

- You are adding a **new measure kind** — new time semantics, trend shape, cut-wide rule, or composition contract.
- Multiple KPIs will use the same YAML **`op:`** name with shared bind/lookback behavior.
- The logic needs **`OpPlugin` flags**: `phase`, `cut_restricted`, `emits_trend`, `shiftable`, `requires_time`.
- Bind-time **desugar** or validation rules apply to a family of measures (`compare` → `pct_change`).

### Do not use a new op when

| Need | Use instead |
|---|---|
| Named scalar formula over existing measures | **Measure function** (`op: fn`) |
| Row-wise column math before agg | **Column function** |
| Experimental series algorithm without stable contract | **Hook** (`op: hook`) |
| Wiring existing kinds in a new KPI | **YAML only** — no Python |

---

## 4. Design principles

### 4.1 Scalability — ops are the catalog’s primary growth surface

Platform ops (`point`, `window`, `trend`, `fn`, `hook`) are `role: platform` — the engine fails to start if they cannot load. Add-on ops (`rank`, `lag`, `ntile`, …) are `role: addon` and can be disabled.

New ops still follow **Tier 3**: implementation in `capabilities/ops/` + row in `ops.yaml`. Do not fork `calc_engine.evaluate()` per op name.

### 4.2 Minimal change

1. Add `OpPlugin` subclass to the correct family file (`combo.py`, `period.py`, or `cut.py`).
2. Register one row in `ops.yaml`.
3. Regenerate `CAPABILITIES.md`.
4. Add bind + compute tests.

Import helpers from `capabilities/ops/support.py` — **not** from `calc_engine` (dependency direction is capabilities → protocol, never reverse).

### 4.3 OpPlugin contract

| Method / attribute | When | Purpose |
|---|---|---|
| `name` | Class attr | Registry key (usually matches YAML `op:`) |
| `phase` | Class attr | `"combo"` or `"cut"` |
| `cut_restricted` | Class attr | Honors `measures.*.cuts` list |
| `requires_time` | Class attr | Reject on snapshot KPIs without `time:` |
| `emits_trend` | Class attr | Returns `(axis, values)` tuple |
| `shiftable` | Class attr | Allow evaluate at shifted anchor (lag of this op) |
| `extra_keys` | Class attr | YAML keys beyond common set |
| `parse()` | Bind | Build `OutputSpec` from raw YAML |
| `validate()` | Bind | Reject illegal wiring |
| `dependencies()` | Bind graph | Other measure keys required |
| `lookback()` / `lookforward()` | Time plan | Months/years to scan |
| `evaluate()` | Combo | Return scalar or trend |
| `source_for_cut()` | Combo | Stash data for cut phase |
| `apply_to_cut()` | Cut | Write values across cut partition |

### 4.4 Three op families

| Family | File | Typical ops | Runs |
|---|---|---|---|
| Combo | `combo.py` | `point`, `window`, `trend`, `fn`, `hook`, `constant` | Per combo, on one group's series |
| Period | `period.py` | `lag`, `lead`, `diff`, `pct_change`, `rolling_*` | Per combo, calendar shifts |
| Cut | `cut.py` | `rank`, `percent_of_total`, `ntile`, `bottom_n` | After all combos on a cut |

Pick the file by **what data the op needs**. If it must see every row on a cut at once → `cut.py`.

### 4.5 Bind-time sugar (not new ops)

`compare`, `filtered_point`, `filtered_window`, `filtered_trend`, `filtered_compare` desugar in `binder.py` to existing ops. Prefer desugar when behavior is compositional — do not add duplicate op kinds.

---

## 5. Comparison with other extension types

| Dimension | Op | Hook | Measure fn | Column fn |
|---|---|---|---|---|
| Extends via | `OpPlugin` class | Callable | Callable | Callable |
| Registry | `ops.yaml` | `hooks.yaml` | `functions/measure.yaml` | `functions/column.yaml` |
| YAML | `op: window` etc. | `op: hook` + `hook:` | `op: fn` | `base_measures.op` |
| Bind hooks | parse, validate, deps, lookback | Via Hook op only | Via Fn/Arithmetic op | Via binder base measure |
| Trend output | Yes (`emits_trend`) | No (scalar) | No | No |
| Cut-wide | Yes (`phase: cut`) | No | No | No |
| Shift anchor | `shiftable` flag | Via trailing/offset | N/A | N/A |

**Op vs hook:** Ops are the productized measure kinds; hooks are the escape hatch for series algorithms. Promote hooks to ops when semantics stabilize.

**Op vs measure fn:** `fn` is itself an op that **dispatches** to `MEASURE_FNS`. A new `fn` name is not a new op — only a new measure **kind** is a new op.

---

## 6. Decision guide

```
New reusable measure behavior?
├─ Composes existing point/window/fn?
│   └─ YAML only — no new op
├─ Scalar math on measure outputs?
│   └─ Measure function (via op: fn)
├─ Row math on fact columns?
│   └─ Column function
├─ Series algorithm, experimental?
│   └─ Hook
├─ New time/window/trend/cut semantics?
│   └─ New OpPlugin
└─ Bind sugar for common pattern (YoY compare)?
    └─ Desugar in binder (Tier 4 — discuss first)
```

---

## 7. Onboarding guide (step-by-step)

### Step 1 — Check the catalog

Open `kpi_engine/registries/CAPABILITIES.md`. Aliases exist (`rolling_median` → `period_median`). `compare` desugars — do not duplicate YoY as a new op.

### Step 2 — Choose family and phase

| Question | Choice |
|---|---|
| Needs all groups on a cut? | `cut.py`, `phase = "cut"` |
| Calendar shift on one series? | `period.py` |
| Default combo behavior? | `combo.py` |

### Step 3 — Implement OpPlugin

Example skeleton in `capabilities/ops/combo.py`:

```python
class MyKind(OpPlugin):
    name = "my_kind"
    phase = "combo"
    cut_restricted = False
    requires_time = True
    emits_trend = False
    shiftable = False
    extra_keys = frozenset({"weight"})

    def parse(self, key, raw, ctx):
        ...

    def validate(self, spec, ctx):
        ...

    def dependencies(self, spec):
        return (spec.of_key,)

    def lookback(self, spec, plan):
        return plan.months(0)

    def evaluate(self, ctx):
        ...
        return scalar_value
```

For cut ops, implement `source_for_cut()` during combo eval and `apply_to_cut()` to assign ranks/shares across the cut partition.

### Step 4 — Register in ops.yaml

```yaml
my_kind:
  role: addon
  enabled: true
  aliases: []
  description: One sentence for CAPABILITIES.md.
  example: |
    my_measure:
      op: my_kind
      of: sotif_value
      weight: 1.0
  module: kpi_engine.capabilities.ops.combo
  attr: MyKind
```

| Key | Required | Notes |
|---|---|---|
| `role` | Yes | `platform` only for engine-critical kinds |
| `module` | Yes | Must start with `kpi_engine.capabilities.` |
| `attr` | Yes | Class name (instantiated at load) |

### Step 5 — Regenerate and test

```python
from kpi_engine.pipeline.loader import write_generated_docs
write_generated_docs()
```

Tests: `tests/test_ops_core.py`, `tests/test_addon_ops.py`, pattern KPIs in `kpi_config/patterns/core_ops.yaml`.

### Do not change

| File | Reason |
|---|---|
| `calc_engine.py` | Generic dispatch |
| `op_registry.py` | Generic register |
| `pipeline/` except rare Tier 4 | New shared YAML fields only |

---

## 8. Worked examples

### Example 1 — Combo op: trailing window (existing `window`)

```yaml
measures:
  value_3m:
    op: window
    of: sotif_value
    trailing: { months: 3 }
    inclusive: true
```

`Window` op plans 3 months lookback, aggregates dense spine values, returns one scalar at anchor.

### Example 2 — Cut op: rank on global cut

```yaml
measures:
  reason_rank:
    op: rank
    of: current_value
    order: desc
    cuts: [G]
```

`Rank` runs in cut phase — compares all combo rows on cut `G`, assigns rank per partition rules.

### Example 3 — Period op via compare sugar

```yaml
measures:
  yoy:
    op: compare
    of: sotif_value
    mode: yoy
```

At bind, `compare` desugars to `pct_change` over lagged series — authors get friendly YAML without a new op implementation.

### Example 4 — Composition ops (not new ops)

These ops **wrap** other capabilities:

| op | Wraps |
|---|---|
| `fn` | `MEASURE_FNS` |
| `arithmetic` | `MEASURE_FNS` (default `divide`) |
| `expr` | measure + column expr evaluators |
| `hook` | `hook_registry.REGISTRY` |

Adding `growth_pct` is a **measure fn**, not a new op. Adding `window` variant with new semantics **is** a new op (or extend `Window` with validated `extra_keys`).

---

## 9. Testing checklist

- [ ] Op loads at startup (`tests/test_capability_registries.py`)
- [ ] `require_op("my_kind")` returns plugin
- [ ] Bind rejects invalid YAML (missing `of:`, illegal combo on snapshot KPI)
- [ ] Lookback plan matches expected scan width
- [ ] `compute()` on parquet — scalar/trend matches oracle
- [ ] Cut op: all rows on cut receive values after `apply_to_cut`
- [ ] `shiftable=False` op rejects shifted anchor if applicable
- [ ] Pattern KPI or gold template in `kpi_config/patterns/` if platform-facing

---

## 10. Common mistakes and anti-patterns

| Mistake | Fix |
|---|---|
| New op for `growth_pct` formula | Measure fn + `op: fn` |
| Importing `calc_engine` from capabilities | Use `support.py` helpers |
| Cut rank logic in combo `evaluate()` | Use `phase="cut"` + `apply_to_cut` |
| Duplicate `compare` as custom op | Use bind desugar |
| `platform` role for experimental op | Start as `addon` |
| One Python file per op name | Add class to family file (`combo.py`, etc.) |

---

## 11. When this is not enough (Tier 4)

Pipeline / contract changes required for:

- New **common measure field** (like `offset`) parsed by all ops
- New **bind desugar** pattern (`filtered_*` family)
- New **agg:** or filter operator
- Changes to **evaluate() dispatch order** or cut/combo composition

These require architecture review and updates to `kpi-framework-plan.md`.

---

## 12. Reference: platform vs addon ops

| Role | Startup if disabled | Examples |
|---|---|---|
| `platform` | Engine fails | `point`, `window`, `trend`, `fn`, `hook`, `arithmetic` |
| `addon` | Skipped silently | `rank`, `lag`, `ntile`, `ewma` (as op if exists), many period helpers |

Use `addon` for new experimental kinds until multiple production KPIs depend on them.

---

## Regenerate Word document

```bash
pip install -e ".[docs]"
python scripts/generate_onboarding_docs.py
```

Output: `docs/onboarding/onboarding-op.docx`
