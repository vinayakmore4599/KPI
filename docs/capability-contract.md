# KPI engine capability contract

Normative semantics for capabilities added in the expansion work. Phase 1–3 locks below. Deferred items stay deferred.

**Exception kinds.** BindError = illegal YAML or illegal request vs YAML (for example a missing required filter). CatalogError = data shape at apply (bad weight column type, missing mask column at runtime, list_agg overflow).

Out of scope for the engine: hierarchy `heir` expansion, cut templates, DuckDB `WHERE` for compound measure masks, host policy (`mask_pii`, `format_pct`, holiday calendars, extract-size env caps).

Large OR / `like` masks keep row detail in Pandas. Prefer a `kind: sql` model (and `identity_grain` when rows are already unique) if the extract is huge. GROUPING SETS pushdown is not in this contract — pre-aggregate in the SQL model until profiled (U1).

---

## Phase 1

### Masks (`where:` / `also_where:`)

Measure and base `where:` run on **detail, then fold**. Cuts do not re-apply those masks. Cut `ignore_filters` does not change a fact that was already masked.

Compound masks (`or`, `and`, `not`, `also_where`, regexp on those paths) evaluate in **Pandas** via `pandas_mask`. DuckDB **SELECT**s the mask columns. DuckDB does **not** compile measure-mask AST into extract `WHERE`. Context `filters:` with `apply: extract` still use `sql_predicate`.

**AST**

- Leaf: `{ column, op, value }` or `{ column, op, values: [...] }` (same as today’s `MeasureWhere`).
- `or` / `and`: non-empty list of masks. Empty list is BindError. Max nesting depth **3**.
- `not`: one mask (leaf or subtree).
- `also_where: [...]` and `where: { and: [...] }` are equivalent. The binder flattens AND onto the cloned base (`where` + `also_where`).

**Ops** (Pandas, same names as `pandas_mask`): `in`, `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `like`, `ilike`, `not_like`, `between`, `not_between`, `is_null`, `is_not_null`, `regexp`, `regexp_insensitive`.

- `regexp` is case-sensitive. `regexp_insensitive` is case-insensitive.
- Pattern length ≤ 256. Invalid pattern is BindError at parse.
- `like` / `regexp` on non-string columns: `astype` string in Pandas. Do not claim DuckDB RE2 parity.
- `is_null` / `is_not_null` arity 0; extra `value:` is BindError.
- Snapshot KPIs may use `filtered_point` plus a mask AST.

Physical models: a mask column missing from datasets / `output_schema` is BindError. A column missing at apply is CatalogError.

### Aggregations

| Agg | Additive across cuts? | Notes |
|-----|------------------------|--------|
| `geomean` | No | Re-read detail per cut |
| `harmonic_mean` | No | Re-read detail per cut |
| `any` / `all` | No | 0 = false, nonzero = true, null skipped; empty or all-null group → null |
| `weighted_avg` | Re-aggregate | Carry `__wsum` / `__wcount` (same pattern as `avg`) |
| `list_agg` / `string_agg` | — | **Not Phase 1** (Phase 3) |

**Weighted average.** `agg: weighted_avg` plus `weight_column:`. Null or zero weight excludes the row. If every weight is null or zero, the group is null.

### Compare vs effective grain

After `apply_request_time`, named compare modes must match **effective** `time.grain`:

| mode | required grain |
|------|----------------|
| `mom` | `month` |
| `wow` | `week` |
| `qoq` | `quarter` |
| `yoy` / `pop` / `diff` / `pct_change` | none for named grain |

Mismatch is BindError. `mode: pop` is the portable prior bucket at the current grain.

**`bind_request(kpi, request)`** (used by `compute` and `validate`): parameter overlay → `apply_request_time` → compare grain-check → desugar compare → lookback. `load_kpi` does **not** desugar compare.

### Host filters

- `required: true`: omitted key or `[]` → BindError. Required is **request-level**: the host must still send a non-blank value if a cut ignores that code. The ignoring cut still does not apply the predicate.
- `default:` applies only when the key is **absent**. Present `[]` still skips (no default injection).
- Same filter cannot set both `required: true` and `default:` (BindError).
- Keep rejecting `optional: false`. Do not implement required by flipping optional.

### inherit_filters / reset_filters

Default (no inherit/reset): unchanged `split_filters` — if **any emitted cut** ignores a code, that filter is calc-stage and skipped on ignoring cuts.

| YAML on cut | Meaning |
|-------------|---------|
| `ignore_filters: [region]` | This cut does not apply region |
| `inherit_filters: [supplier]` | This cut **must apply** supplier. BindError if `supplier` is also in this cut’s `ignore_filters`. Does not pull supplier into DuckDB if another **emitted** cut ignores it (extract stays wide; apply at calc on this cut) |
| `reset_filters: [supplier]` | This cut does not apply supplier; equivalent to adding it to `ignore_filters` for this cut only |
| Code in both inherit and ignore | BindError |

`filters[].apply: extract\|calc\|result` is unchanged. inherit/reset only change per-cut apply vs skip.

---

## Phase 2 — cuts, row pipeline, core catalog

### Cut control precedence

`only_cut` > `emit_cuts` ∩ walk > `locked_cut` root > `default_cut` > `also_emit`.

`parameters.output_cut` remains a walk root, not a hard lock, unless `only_cut` is set. **`emit_cuts` never removes the walk root** (`output_cut` / `only_cut`); it only filters `also_emit` extras.

Dependency cuts for `versus_cut` / `from_cut` are **silent** (computed, not emitted) unless they are also on the walk.

### `versus_cut` vs `from_cut`

- **`versus_cut`:** denominator / total of `of:` on another cut. Share-like only: `percent_of_total`, `rank`, `contribution`, `bottom_n`. Not `diff` / `pct_change`.
- **`from_cut`:** scalar reuse. Broadcast a coarser-cut scalar onto finer rows on **shared dims**. Finer-to-coarser is BindError. Cannot combine with `versus_cut`.

### Row pipeline

- `lookup.keys` for composite maps (not with `column:`).
- Effective-dated lookup: `valid_from` / `valid_to`; default as-of is the request **anchor**; `as_of: row_column` or `as_of: anchor`.
- `trend.partition_by` allowed; overflow is existing `TREND_CELL_CAP` CatalogError.

### `omit_null_rows`

`0` and `false` are values. Only JSON **null** counts. Trend measures are excluded unless only trends were requested.

---

## Phase 3 — advanced catalog, governance, multi-view

### Dual keys (no nested JSON in one key)

`op: band` / `op: envelope` / `hook: forecast_confidence` expand to `{key}_low` and `{key}_high` unless `emit:` names one side. `abc_class` is a string scalar (`A`/`B`/`C`).

### `list_agg` / `string_agg`

Non-additive. Caps 1000 items / 64KB. Overflow is **CatalogError**. `of:` only `point`. Empty window is **null**.

### `compound_growth` vs `hook: cagr`

`compound_growth` = fixed N periods. `cagr` = trailing observed series. Pick one shape.

### Cohort / survival hooks

Require registry `extra_keys`: `cohort_column`, `entry_period`.

### Governance

KPI `having` drops groups first. Measure `having` nulls that measure without dropping the row. Measure `required: true` → notes `required_measure_null` (not BindError). Orchestrator merges `quality_flags`. Trend pagination is per measure key; `trend_axes` stays full. `sort:` / `max_rows` run post-eval.

### Multi-view

Opt-in `execution.multi_view: true`. Envelope `{ multi_view: true, views: [{ view_id, ok, result | error }] }`. Same `kpi_id` only. `fail_fast: true` re-raises. Shared extract cache key `(kpi_id, model, filters_hash, span)` is optional host-side reuse (v1 still extracts per view).

### Deferred

`share_of_parent`, multi-level cuts, `peer_group` wait on `dimensions[].parent:`. `heir` stays rejected in the adapter.

