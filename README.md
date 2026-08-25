# KPI Engine

Config-driven KPI calculator. It consumes a **context JSON** from the existing metadata framework, loads data with **DuckDB**, calculates in **Pandas**, and returns **JSON** (table scalars plus optional trend arrays for graphs).

You onboard a KPI by adding YAML. You should not need to change engine code for a normal metric. A new reusable **name** (op, hook, column function, measure function) is two folders — `capabilities/` plus `registries/` — not `core/`.

**Onboarding playbook (steps + which files to change):** [kpi-onboarding-guide.md](kpi-onboarding-guide.md)

**YAML preparation (AI brief + human deep-dive):** [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md) — attach §0 plus the calculation intake to any AI; it must emit a complete KPI YAML. Same contract on the **YAML preparation** sheet of `docs/KPI-Engine-Capabilities.xlsx`.

**YAML reference (every op, aggregation and key):** [kpi-yaml-reference.md](kpi-yaml-reference.md)

**Live catalog** (every registered name): [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md)

Full architecture: [kpi-framework-plan.md](kpi-framework-plan.md).

Every Python and YAML file starts with a header covering **what it provides**, **where it is used**, **capabilities**, and **when to change it**.

---

## Folders

```text
udfs/                         Copy this folder into the platform
  kpi_engine/
    main.py                   Entry: udfs.kpi_engine.main → compute(context)
    core/                     Frozen engine (adapt, bind, extract, dispatch)
    capabilities/             Op / function / hook bodies
    registries/               YAML allowlist + generated CAPABILITIES.md
    contracts.py              Shared typed fields
  config/
    kpis/<kpi_group>/         One YAML per kpi_id (group is authoring only)
    models/<kpi_group>/       DuckDB extract (tables/joins or SQL)

tests/                        Local parquet tests (no ADLS) — not deployed
```

| You want to… | Open |
|---|---|
| Add or change a KPI | `udfs/config/kpis/<kpi_group>/<id>.yaml` |
| Change how source tables join | `udfs/config/models/<kpi_group>/<name>.yaml` |
| Understand a request failure | `udfs/kpi_engine/core/` (adapter, binder, time_planner, loader) |
| Add a reusable name (op / hook / function) | `udfs/kpi_engine/capabilities/` + `registries/` ([catalog](udfs/kpi_engine/registries/CAPABILITIES.md)) |
| Add an `agg`, filter operator, or common YAML field | `udfs/kpi_engine/core/` (and `contracts.py` for a shared field) |

---

## Install and test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

---

## Run a request

The metadata layer already builds `context`. This engine only consumes it. DuckDB and ADLS stay on the platform — `compute` / `udfs.kpi_engine.main` reuse that connection.

```python
from kpi_engine import compute, validate
from kpi_engine.main import main

# Compile DuckDB SQL without scanning files
validate(context)

# Full calculation (pass the platform DuckDB session when the host has one)
result = compute(context, connection=platform_connection)
# or the UDF entry:
result = main(context, connection=platform_connection)
```

Each `compute` / `validate` writes a new file under `logs/` (or `$KPI_ENGINE_LOG_DIR`):

`logs/kpi-compute-<kpi_id>-<YYYYMMDD-HHMMSS-ffffff>-<seq>.log`

The file traces every pipeline step (adapt, bind, extract, calculate), logs the **full DuckDB SQL** (parameterized, each bound value, and the same statement with values inlined so you can paste it into DuckDB), and records each function invoke/return. Pass `log_dir=` to override the folder. Set `KPI_ENGINE_LOG=0` to disable.

`business_date` on the context is ignored. The **selected month** in filters is the anchor.

---

## KPI YAML (what the sections mean)

Example: `udfs/config/kpis/sotif/3004.yaml`.

**`dimensions`** — columns that split rows (`reason_code`, `region`). Not numbers.

**`base_measures`** — internal fact from the table, e.g. `sotif_value` from column `amount` with `agg: sum` (Pandas). The UI does not request this name. Aggregations: `sum`, `avg`, `count`, `min`, `max`, `count_distinct`, `median`, `percentile`.

**`measures`** — calculated columns the UI can request via `measure_key`. The live list of ops, functions, and hooks is [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md). Common kinds:

- `point` — one period (current, previous year)
- `window` — trailing 3 / 6 / 12 periods (and similar)
- `trend` — array of per-period values for a graph
- `arithmetic` / `fn` / `expr` — combine other measures
- `dimension` — only if the context still sends a dimension as `measure_key`
- `hook` — an allowlisted Python function for logic the catalog cannot express

**`cuts`** — grouping grains, not measures. Example: **G** = global (no region), **R** = by region. `also_emit` packs extra grains into the same response.

**`time.filter_code`** — which context filter is the selected month. That filter is **never** applied as `IN (one month)`; it becomes a date **range** so lookbacks have history.

---

## Request path

1. Adapt context (one view, `value`/`values` → IN lists).
2. Load KPI + model YAML; bind dataset **alias** to context paths.
3. Claim the month filter → anchor + `required_span`.
4. DuckDB: scan, source IN filters, time range, `GROUP BY` month grain.
5. Pandas: dense month spine, cuts, then each requested measure via its registered plugin.
6. JSON: one row per dimension combo per cut; one column per requested `measure_key`.

---

## Adding a KPI

1. Copy `udfs/config/kpis/sotif/3004.yaml` to `udfs/config/kpis/<kpi_group>/<kpi_id>.yaml`.
2. Point `model` at an alias that exists in context datasets.
3. Declare `dimensions`, `base_measures`, `cuts`, and `measures` (every `measure_key` the page can ask for).
4. Run `validate(sample_context)` then `pytest`.

Do not put ADLS paths, YoY math, or DuckDB connection code in the KPI file.
