"""Per-run timestamped log files: steps, full SQL, invoke/return.

What this file provides
    compute/validate write a new log file per call. The file contains STEP
    banners, the entire DuckDB query, INVOKE/RETURN lines, and MEASURE results.

Where it is used
    pytest tests/test_runlog.py.

When to use
    Add a case when a new pipeline phase should appear in the trace file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError
from tests.conftest import make_context, value_of


def test_compute_writes_a_timestamped_log_with_sql_and_steps(parquet_path, config_dir, tmp_path):
    """One compute() → one log file named with kind, kpi_id, and a timestamp."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir, log_dir=log_dir)
    files = list(log_dir.glob("kpi-compute-3004-*.log"))
    assert len(files) == 1
    name = files[0].name
    assert re.match(r"kpi-compute-3004-\d{8}-\d{6}-\d{6}-\d{4}\.log", name)
    text = files[0].read_text(encoding="utf-8")
    assert "STEP START compute" in text
    assert "STEP adapt" in text
    assert "STEP bind" in text
    assert "STEP plan_time" in text
    assert "STEP extract" in text
    assert "STEP calculate" in text
    assert "STEP END compute" in text
    assert "INVOKE kpi_engine.pipeline.adapter.adapt" in text
    assert "RETURN kpi_engine.pipeline.adapter.adapt" in text
    assert "INVOKE kpi_engine.pipeline.model_sql.extract" in text
    assert "---------- SQL" in text
    assert "SELECT" in text
    assert "FROM" in text
    assert "GROUP BY" not in text
    assert "---------- END SQL ----------" in text
    assert "MEASURE" in text
    assert "current_value" in text
    assert "RUN end" in text
    assert "---------- CONTEXT received ----------" in text
    assert '"kpi_id": 3004' in text or '"kpi_id":3004' in text
    assert str(parquet_path) in text
    assert "reporting_month" in text
    assert "current_value" in text
    assert "---------- END CONTEXT ----------" in text
    assert result["rows"]


def test_each_run_gets_its_own_log_file(parquet_path, config_dir, tmp_path):
    """Two computes produce two distinct timestamped files."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    compute(ctx, config_dir=config_dir, log_dir=log_dir)
    compute(ctx, config_dir=config_dir, log_dir=log_dir)
    files = sorted(log_dir.glob("kpi-compute-3004-*.log"))
    assert len(files) == 2
    assert files[0].name != files[1].name
    assert files[0].read_text() != "" and files[1].read_text() != ""


def test_validate_logs_the_full_compiled_query(parquet_path, config_dir, tmp_path):
    """validate() also writes a run file containing the entire SQL."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["value_3m"], supplier=["ABC"])
    planned = validate(ctx, config_dir=config_dir, log_dir=log_dir)
    files = list(log_dir.glob("kpi-validate-3004-*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "STEP START validate" in text
    assert planned["sql"] in text
    assert "date_trunc" in text
    assert "---------- PARAMS" in text


def test_failures_are_written_to_the_same_run_file(parquet_path, config_dir, tmp_path):
    """A bind error still produces a log that includes the traceback."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["not_a_real_measure"])
    try:
        compute(ctx, config_dir=config_dir, log_dir=log_dir)
    except BindError:
        pass
    else:
        raise AssertionError("expected BindError")
    files = list(Path(log_dir).glob("kpi-compute-3004-*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "compute failed" in text
    assert "BindError" in text
    assert "not_a_real_measure" in text


def test_full_received_context_is_logged(parquet_path, config_dir, tmp_path):
    """The log contains the entire inbound context JSON, not a summary."""
    import json

    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    compute(ctx, config_dir=config_dir, log_dir=log_dir)
    text = next(log_dir.glob("kpi-compute-3004-*.log")).read_text(encoding="utf-8")
    start = text.index("---------- CONTEXT received ----------")
    end = text.index("---------- END CONTEXT ----------")
    block = text[start:end]
    dumped = json.dumps(ctx, indent=2, default=str, ensure_ascii=False)
    assert dumped in block
    assert ctx["datasets"]["Sotif"]["path"] in block
    assert "current_value" in block
    assert "ABC" in block


def test_sql_in_the_log_is_not_truncated(parquet_path, config_dir, tmp_path):
    """The log file contains the same SQL string compute returns, in full."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["current_value", "value_3m"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir, log_dir=log_dir)
    text = next(log_dir.glob("kpi-compute-3004-*.log")).read_text(encoding="utf-8")
    assert result["sql"]
    assert result["sql"] in text
    for sql in result["sqls"]:
        assert sql in text
    assert "---------- SQL BOUND (values inlined; copy into DuckDB) ----------" in text
    start = text.index("---------- SQL BOUND (values inlined; copy into DuckDB) ----------")
    end = text.index("---------- END SQL ----------")
    bound = text[start:end]
    assert "read_parquet(" in bound
    assert str(parquet_path) in bound
    assert "?" not in bound
    assert "DATE '" in bound
    assert "ABC" in bound


def test_percent_in_path_does_not_drop_sql_from_the_log(parquet_path, config_dir, tmp_path):
    """A `%` in a dataset path must not make the logging formatter skip the SQL block."""
    weird = tmp_path / "sotif%20facts.parquet"
    weird.write_bytes(Path(parquet_path).read_bytes())
    log_dir = tmp_path / "logs"
    ctx = make_context(weird, measures=["current_value"], supplier=["ABC"])
    result = compute(ctx, config_dir=config_dir, log_dir=log_dir)
    text = next(log_dir.glob("kpi-compute-3004-*.log")).read_text(encoding="utf-8")
    assert result["sql"] in text
    assert str(weird) in text
    assert "sotif%20facts.parquet" in text
    assert "---------- END SQL ----------" in text


def test_measure_calc_log_includes_columns_used_values_and_result(
    parquet_path, config_dir, tmp_path
):
    """Each measure writes the column, the periods/inputs it read, and the result."""
    log_dir = tmp_path / "logs"
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value", "yoy_month", "value_3m"],
        supplier=["ABC"],
    )
    result = compute(ctx, config_dir=config_dir, log_dir=log_dir)
    text = next(log_dir.glob("kpi-compute-3004-*.log")).read_text(encoding="utf-8")
    assert "---------- MEASURE calculate ----------" in text
    assert "---------- END MEASURE ----------" in text
    assert "key=current_value op=point" in text
    assert "of=sotif_value column=sotif_value source=amount agg=sum" in text
    assert "period=2026-03-01" in text
    assert "used:" in text
    assert "sotif_value=" in text
    assert "key=previous_year_value op=point" in text
    assert "period=2025-03-01" in text
    assert "key=value_3m op=window" in text
    assert "window=2026-01-01 .. 2026-03-01" in text
    assert "key=yoy_month op=arithmetic" in text
    assert "fn=growth_pct" in text
    assert "inputs:" in text
    assert "current_value=" in text
    assert "previous_year_value=" in text
    g = next(
        row
        for row in result["rows"]
        if row["output_cut"] == "G" and row["reason_code"] == "LATE_SUPPLIER"
    )
    assert f"result={value_of(g, 'current_value')!r}" in text or f"result={g['current_value']!r}" in text


def test_result_json_is_off_by_default(parquet_path, config_dir, tmp_path):
    """KPI_ENGINE_RESULT_LOG defaults off; compute does not write a result file."""
    dest = tmp_path / "results"
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    compute(ctx, config_dir=config_dir, result_log_dir=dest)
    assert list(dest.glob("kpi-result-*.json")) == []


def test_result_json_via_kwarg(parquet_path, config_dir, tmp_path):
    """compute(..., result_log=True) writes the wrapped payload."""
    dest = tmp_path / "results"
    ctx = make_context(
        parquet_path,
        measures=["current_value", "previous_year_value"],
        supplier=["ABC"],
    )
    result = compute(
        ctx, config_dir=config_dir, log_dir=tmp_path / "logs", result_log=True, result_log_dir=dest
    )
    files = list(dest.glob("kpi-result-3004-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["kpi_id"] == result["kpi_id"]
    g = next(
        row
        for row in payload["rows"]
        if row["output_cut"] == "G" and row["reason_code"] == "LATE_SUPPLIER"
    )
    assert g["previous_year_value"] == {"value": 15.0, "period": "2025-03-01"}
    logs = list((tmp_path / "logs").glob("kpi-compute-3004-*.log"))
    assert logs
    stamp_seq = files[0].name[len("kpi-result-3004-") : -len(".json")]
    assert logs[0].name == f"kpi-compute-3004-{stamp_seq}.log"


def test_result_json_via_env(parquet_path, config_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("KPI_ENGINE_RESULT_LOG", "1")
    dest = tmp_path / "results"
    ctx = make_context(
        parquet_path,
        measures=["previous_year_value"],
        supplier=["ABC"],
    )
    compute(ctx, config_dir=config_dir, result_log_dir=dest)
    files = list(dest.glob("kpi-result-3004-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    g = next(
        row
        for row in payload["rows"]
        if row["output_cut"] == "G" and row["reason_code"] == "LATE_SUPPLIER"
    )
    assert g["previous_year_value"]["period"] == "2025-03-01"


def test_result_log_false_wins_over_env(parquet_path, config_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("KPI_ENGINE_RESULT_LOG", "1")
    dest = tmp_path / "results"
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    compute(ctx, config_dir=config_dir, result_log=False, result_log_dir=dest)
    assert list(dest.glob("kpi-result-*.json")) == []


def test_render_bound_sql_skips_placeholders_inside_quotes():
    """A `?` in a string literal is not a parameter."""
    from kpi_engine.runlog import render_bound_sql

    sql = "SELECT 'what?' AS q, x FROM t WHERE id = ?"
    assert render_bound_sql(sql, [7]) == "SELECT 'what?' AS q, x FROM t WHERE id = 7"
