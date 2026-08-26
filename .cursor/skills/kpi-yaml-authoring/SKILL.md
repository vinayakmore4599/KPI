---
name: kpi-yaml-authoring
description: >-
  Create or edit KPI YAML and model YAML for the kpi engine. Use when the user
  asks to onboard a KPI, write kpi_config/kpis or kpi_config/models YAML,
  fix BindError in KPI config, map a calculation spec to YAML, or mentions
  kpi-yaml-ai-prep, measure_key, base_measures, cuts, or model_id.
---

# KPI YAML authoring

Follow this skill whenever you create or edit KPI/model YAML in this repo.

## Required reading (in order)

1. **[kpi-yaml-ai-prep.md](../../kpi-yaml-ai-prep.md)** — bind-ready contract. Read fully before emitting YAML.
2. **[kpi_engine/registries/CAPABILITIES.md](../../kpi_engine/registries/CAPABILITIES.md)** — live op/fn/hook names (wins over static lists).
3. Gold examples: [kpi_config/kpis/sotif/3004.yaml](../../kpi_config/kpis/sotif/3004.yaml), [kpi_config/models/sotif/sotif.yaml](../../kpi_config/models/sotif/sotif.yaml).

For key-by-key detail: [kpi-yaml-reference.md](../../kpi-yaml-reference.md).

## Hard rules (do not skip)

- **Two layers:** model = DuckDB extract only; KPI = time/dims/cuts/measures. Never put `agg:`/`op:` in model files or SQL in KPI files.
- **Ratios:** two `base_measures` with `agg: sum` (or appropriate agg), then measure-level `expr`/`fn`/`arithmetic`. Never `agg: avg` on a per-row ratio.
- **Offset/trailing keys:** plural only — `years`, `months`, `quarters`, `weeks`, `days`, `periods`. Singular (`year:`, `month:`) is BindError.
- **Cuts:** `group_by` = extras only. Pair `ignore_filters: [dim]` with `exclude_from_grain: [dim]`.
- **Shares:** `op: percent_of_total`, not `fn: percent`.
- **Period compare:** `op: compare` + `mode:` on a named base. Physical-column mask: `filtered_*` + `column:`. Filter + compare in one key: `filtered_compare`. Not `compare` + `column:`.
- **Trends of ratios:** `op: trend_arithmetic`, not `op: trend` on a composite.
- **Closed names only** — if not in CAPABILITIES.md, stop and report the gap; do not invent ops.

## Output format

1. Full `kpi_config/kpis/<group>/<kpi_id>.yaml`
2. Full `kpi_config/models/<group>/<model_id>.yaml` **only if** extract is new; else one sentence reusing existing model
3. Assumptions + gaps (ask numbered questions if intake incomplete — **no partial YAML**)

## Validate mentally against §11 checklist in ai-prep

Before finishing, confirm every checklist row in `kpi-yaml-ai-prep.md` §11 is true.

## Optional local validation

```bash
pytest tests/test_yaml_validation.py -q
```

Run against fixtures when you add new YAML under `kpi_config/` or test configs.
