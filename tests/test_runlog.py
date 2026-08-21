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

import re
from pathlib import Path

from kpi_engine import compute, validate
from kpi_engine.exceptions import TimePlanError
from tests.conftest import make_context


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
    assert "INVOKE kpi_engine.core.adapter.adapt" in text
    assert "RETURN kpi_engine.core.adapter.adapt" in text
    assert "INVOKE kpi_engine.core.model_sql.extract" in text
    assert "---------- SQL" in text
    assert "SELECT" in text
    assert "FROM" in text
    assert "GROUP BY" in text
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
    """A missing month filter still produces a log that includes the traceback."""
    log_dir = tmp_path / "logs"
    ctx = make_context(parquet_path, measures=["current_value"])
    del ctx["filters"]["reporting_month"]
    try:
        compute(ctx, config_dir=config_dir, log_dir=log_dir)
    except TimePlanError:
        pass
    else:
        raise AssertionError("expected TimePlanError")
    files = list(Path(log_dir).glob("kpi-compute-3004-*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "compute failed" in text
    assert "TimePlanError" in text
    assert "reporting_month" in text


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
