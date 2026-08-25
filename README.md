# KPI Engine

Config-driven KPI calculator. It consumes a **context JSON** from the existing metadata framework, loads data with **DuckDB**, calculates in **Pandas**, and returns **JSON** (table scalars plus optional trend arrays for graphs).

You onboard a KPI by adding YAML. You should not need to change engine code for a normal metric. A new reusable **name** (op, hook, column function, measure function) is two folders — `capabilities/` plus `registries/` — not `pipeline/`.

**Onboarding playbook (steps + which files to change):** [kpi-onboarding-guide.md](kpi-onboarding-guide.md)

**YAML preparation for AI (give this to a model):** [kpi-yaml-ai-prep.md](kpi-yaml-ai-prep.md) — short bind-ready contract. Human deep-dive: [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md). Catalogs also on `docs/KPI-Engine-Capabilities.xlsx`.

**YAML reference (every op, aggregation and key):** [kpi-yaml-reference.md](kpi-yaml-reference.md)

**Live catalog** (every registered name): [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md)

**How the engine runs (end-to-end flow + diagrams):** [kpi-system-architecture.md](kpi-system-architecture.md)

Locked architecture decisions: [kpi-framework-plan.md](kpi-framework-plan.md).

Every Python and YAML file starts with a header covering **what it provides**, **where it is used**, **capabilities**, and **when to change it**.

---

## Folders

```text
kpi_engine/                   Python package. Host module_path: kpi_engine.main
  main.py                     UDF entry → compute(context)
  pipeline/                   Frozen engine (adapt, bind, extract, dispatch)
  capabilities/               Op / function / hook bodies
  registries/                 YAML allowlist + generated CAPABILITIES.md
  contracts.py                Shared typed fields
kpi_config/                   Default YAML root (or set KPI_ENGINE_CONFIG_DIR)
  kpis/<kpi_group>/           One YAML per kpi_id (group is authoring only)
  models/<kpi_group>/         DuckDB extract (tables/joins or SQL)

tests/                        Local parquet tests (no ADLS) — not deployed
```

Copy **`kpi_engine/`** into the host (next to Hub `core/`, or anywhere importable). Copy **`kpi_config/`** as well, or set `KPI_ENGINE_CONFIG_DIR` to the folder that contains `kpis/` and `models/`. Deploy matching engine + YAML together. Do not put the `kpi_engine/` directory itself on `sys.path` — put its **parent** on the path (that also avoids shadowing Hub packages such as `core` or a host `pipeline`).

| You want to… | Open |
|---|---|
| Add or change a KPI | `kpi_config/kpis/<kpi_group>/<id>.yaml` |
| Change how source tables join | `kpi_config/models/<kpi_group>/<name>.yaml` |
| Understand a request failure | `kpi_engine/pipeline/` (adapter, binder, time_planner, loader) |
| Add a reusable name (op / hook / function) | `kpi_engine/capabilities/` + `registries/` ([catalog](kpi_engine/registries/CAPABILITIES.md)) |
| Add an `agg`, filter operator, or common YAML field | `kpi_engine/pipeline/` (and `contracts.py` for a shared field) |

DuckDB: pass `connection=` into `main` / `compute`, or set `HOST_DUCKDB_GETTER` / `KPI_ENGINE_DUCKDB_GETTER` to `module.path:function`. Do not import Hub `core` from this package.

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

The metadata layer already builds `context`. This engine only consumes it. DuckDB and ADLS stay on the platform — `compute` / `kpi_engine.main` reuse that connection.

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

Example: `kpi_config/kpis/sotif/3004.yaml`.

**`dimensions`** — columns that split rows (`reason_code`, `region`). Not numbers.

**`base_measures`** — internal fact from the table, e.g. `sotif_value` from column `amount` with `agg: sum` (Pandas). The UI does not request this name. Aggregations: `sum`, `avg`, `count`, `min`, `max`, `count_distinct`, `median`, `percentile`.

**`measures`** — calculated columns the UI can request via `measure_key`. The live list of ops, functions, and hooks is [kpi_engine/registries/CAPABILITIES.md](kpi_engine/registries/CAPABILITIES.md). Common kinds:

- `point` — one period (current, previous year)
- `window` — trailing 3 / 6 / 12 periods (and similar)
- `trend` — array of per-period values for a graph
- `arithmetic` / `fn` / `expr` — combine other measures
- `dimension` — only if the context still sends a dimension as `measure_key`
- `hook` — an allowlisted Python function for logic the catalog cannot express

**`cuts`** — grouping grains, not measures. Example: **G** = global (no region), **R** = by region. `also_emit` packs extra grains into the same response.

**`time.filter_code` / `time.periods`** — which context filters define the selected periods. A scalar `filter_code` still wins if present. Those keys are **never** applied as `IN (one month)`; they become a date **range** so lookbacks have history. Independent `periods` parts conjoin (year alone = full year).

---

## Request path

1. Adapt context (one view, `value`/`values` → IN lists).
2. Load KPI + model YAML; bind dataset **alias** to context paths.
3. Claim time filters → selection S, `anchor = max(S)`, `required_span`. Missing parts probe the data.
4. DuckDB: scan, source IN filters, time range, `GROUP BY` month grain.
5. Pandas: dense month spine, cuts, then each requested measure via its registered plugin.
6. JSON: one row per dimension combo per cut; one column per requested `measure_key`.

---

## Adding a KPI

1. Copy `kpi_config/kpis/sotif/3004.yaml` to `kpi_config/kpis/<kpi_group>/<kpi_id>.yaml`.
2. Point `model` at an alias that exists in context datasets.
3. Declare `dimensions`, `base_measures`, `cuts`, and `measures` (every `measure_key` the page can ask for).
4. Run `validate(sample_context)` then `pytest`.

Do not put ADLS paths, YoY math, or DuckDB connection code in the KPI file.
