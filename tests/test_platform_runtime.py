"""Platform DuckDB session: host helper, no new connect, Delta vs Parquet scans.

What this file provides
    compute() reuses a caller/host connection and does not close it.
    Local tests still work with duckdb.connect() when no helper is registered.
    SQL $alias_scan follows context table_type (delta_scan vs read_parquet).

Where it is used
    pytest tests/test_platform_runtime.py.

When to use
    Add a case when the host connection helper or scan token changes.
"""

from __future__ import annotations

import duckdb
import pytest

from kpi_engine import compute, validate
from kpi_engine.exceptions import BindError, KPIEngineError
from kpi_engine.platform import register_duckdb_getter
from tests.conftest import find_row, make_context, minimal_kpi, write_yaml
from udfs.sotif import main


@pytest.fixture(autouse=True)
def _reset_host_getter():
    """Do not leak a registered helper from one test into the next."""
    yield
    register_duckdb_getter(None)


def test_compute_reuses_caller_connection_and_leaves_it_open(parquet_path, config_dir, monkeypatch):
    """The platform session is used for the extract and is not closed afterwards."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    connection = duckdb.connect()

    def boom():
        raise AssertionError("must not open a new DuckDB session")

    monkeypatch.setattr("kpi_engine.platform.duckdb.connect", boom)
    monkeypatch.setattr("kpi_engine.core.model_sql.duckdb.connect", boom)
    try:
        result = compute(ctx, config_dir=config_dir, connection=connection)
        assert result["rows"]
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


def test_registered_host_getter_is_used_and_not_closed(parquet_path, config_dir, monkeypatch):
    """A registered platform helper supplies the session; compute does not close it."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    host = duckdb.connect()
    calls: list[int] = []

    def getter():
        calls.append(1)
        return host

    register_duckdb_getter(getter)
    monkeypatch.setattr("kpi_engine.platform.duckdb.connect", lambda: (_ for _ in ()).throw(AssertionError("no local connect")))
    monkeypatch.setattr("kpi_engine.core.model_sql.duckdb.connect", lambda: (_ for _ in ()).throw(AssertionError("no extract connect")))
    try:
        result = compute(ctx, config_dir=config_dir)
        assert calls == [1]
        assert result["rows"]
        assert host.execute("SELECT 1").fetchone() == (1,)
    finally:
        register_duckdb_getter(None)
        host.close()


def test_local_fallback_closes_the_session_it_opened(parquet_path, config_dir, monkeypatch):
    """Tests with no host helper still run, and that local session is closed."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    wrappers: list[_CloseTrackingConnection] = []
    real = duckdb.connect

    def tracking():
        wrap = _CloseTrackingConnection(real())
        wrappers.append(wrap)
        return wrap

    monkeypatch.setattr("kpi_engine.platform.duckdb.connect", tracking)
    register_duckdb_getter(None)
    result = compute(ctx, config_dir=config_dir)
    assert result["rows"]
    assert len(wrappers) == 1
    assert wrappers[0].closed is True


class _CloseTrackingConnection:
    """Delegates to DuckDB; records close() because the real close is read-only."""

    def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
        self._inner = inner
        self.closed = False

    def execute(self, *args: object, **kwargs: object):
        return self._inner.execute(*args, **kwargs)

    def close(self) -> None:
        self.closed = True
        self._inner.close()


def test_bad_host_getter_spec_is_an_error(parquet_path, config_dir, monkeypatch):
    """A configured helper that cannot be imported fails loudly instead of silent local connect."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    register_duckdb_getter(None)
    monkeypatch.setattr("kpi_engine.platform.HOST_DUCKDB_GETTER", "not_a_real_pkg:get_connection")
    try:
        compute(ctx, config_dir=config_dir)
    except KPIEngineError as exc:
        assert "not_a_real_pkg:get_connection" in str(exc)
    else:
        raise AssertionError("expected KPIEngineError")


def test_main_forwards_platform_connection(parquet_path, config_dir):
    """udfs.sotif.main passes the host connection through to compute."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    connection = duckdb.connect()
    try:
        result = main(ctx, config_dir=str(config_dir), connection=connection)
        assert result["rows"]
        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


def test_sql_alias_scan_follows_table_type(parquet_path, extra_config):
    """$sotif_scan becomes delta_scan(?) or read_parquet(?) from context.table_type."""
    write_yaml(
        extra_config / "models" / "scan_sql.yaml",
        {
            "model_id": "scan_sql",
            "kind": "sql",
            "required_aliases": ["sotif"],
            "output_schema": [
                {"name": "event_month", "type": "date"},
                {"name": "region", "type": "varchar"},
                {"name": "reason_code", "type": "varchar"},
                {"name": "supplier_name", "type": "varchar"},
                {"name": "amount", "type": "decimal"},
            ],
            "sql": "SELECT event_month, region, reason_code, supplier_name, amount FROM $sotif_scan",
        },
    )
    write_yaml(extra_config / "kpis" / "9601.yaml", minimal_kpi(9601, model="scan_sql"))
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"], kpi_id=9601)

    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert "read_parquet(?)" in sql
    assert "delta_scan" not in sql
    g = find_row(
        compute(ctx, config_dir=extra_config),
        cut="G",
        reason="LATE_SUPPLIER",
    )
    assert g["current_value"] == 45.0

    ctx["datasets"]["Sotif"]["table_type"] = "DELTA"
    ctx["datasets"]["Sotif"]["path"] = "abfss://command@account.dfs.core.windows.net/sotif"
    sql = validate(ctx, config_dir=extra_config)["sql"]
    assert "delta_scan(?)" in sql
    assert "read_parquet" not in sql


def test_unbound_alias_scan_fails_at_compile(parquet_path, extra_config):
    """$alias_scan must name a bound model alias."""
    write_yaml(
        extra_config / "models" / "bad_scan.yaml",
        {
            "model_id": "bad_scan",
            "kind": "sql",
            "required_aliases": ["sotif"],
            "output_schema": [{"name": "event_month", "type": "date"}],
            "sql": "SELECT event_month FROM $missing_scan",
        },
    )
    write_yaml(extra_config / "kpis" / "9602.yaml", minimal_kpi(9602, model="bad_scan"))
    ctx = make_context(parquet_path, measures=["current_value"], kpi_id=9602)
    try:
        validate(ctx, config_dir=extra_config)
    except BindError as exc:
        assert "missing_scan" in str(exc) or "$missing_scan" in str(exc)
    else:
        raise AssertionError("expected BindError")


def test_host_getter_spec_requires_module_and_function(parquet_path, config_dir, monkeypatch):
    """HOST_DUCKDB_GETTER must be module.path:function, not a bare name."""
    ctx = make_context(parquet_path, measures=["current_value"], supplier=["ABC"])
    monkeypatch.setattr("kpi_engine.platform.HOST_DUCKDB_GETTER", "just_a_module")
    try:
        compute(ctx, config_dir=config_dir)
    except KPIEngineError as exc:
        assert "module.path:function_name" in str(exc)
    else:
        raise AssertionError("expected KPIEngineError")
