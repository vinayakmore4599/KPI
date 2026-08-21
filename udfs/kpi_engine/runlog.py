"""Per-run file logging for the KPI engine.

What this file provides
    start_run / end_run — one timestamped log file per compute() or validate().
    traced — decorator that records invoke + return for pipeline functions.
    log_sql — writes the DuckDB query, bound parameters, and the same SQL with
    values inlined so it can be copied into DuckDB. Never truncated.
    log_context — writes the inbound context JSON in full, never truncated.
    log_step / log_measure — readable banners for orchestrator phases.

Where it is used
    orchestrator starts a run; core functions use @traced. Tests isolate files
    via KPI_ENGINE_LOG_DIR or compute(..., log_dir=...).

Capabilities
    Default directory is ./logs (or $KPI_ENGINE_LOG_DIR). Disable with
    KPI_ENGINE_LOG=0 when no log_dir is passed. A host session is never
    described beyond its type. SQL (parameterized and inlined) and the inbound
    context are always written in full. Percent signs in SQL or JSON cannot drop
    a log line.

When to use
    Change formatting here, not by scattering print() in core modules.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import is_dataclass, fields
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

_PREVIEW_ROWS = 20
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_FILE_COUNTER = itertools.count(1)
_active: ContextVar["_Run | None"] = ContextVar("kpi_runlog", default=None)


class _SafeFormatter(logging.Formatter):
    """Format a record without re-interpreting % inside the message.

    The default logging formatter does `fmt % record.__dict__` after the
    message is already interpolated. A `%` in SQL or JSON then drops the line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Timestamp, level, message, and optional traceback — message is literal."""
        record.message = record.getMessage()
        stamp = self.formatTime(record, self.datefmt)
        line = f"{stamp}.{int(record.msecs):03d} {record.levelname} {record.message}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                line = f"{line}\n{record.exc_text}"
        return line


class _Run:
    """One compute/validate log file and its logging.Logger."""

    def __init__(self, path: Path, logger: logging.Logger, handler: logging.Handler) -> None:
        self.path = path
        self.logger = logger
        self.handler = handler


def logging_enabled(*, log_dir: str | Path | None = None) -> bool:
    """File logging is on unless KPI_ENGINE_LOG=0 and no explicit log_dir was given."""
    if log_dir is not None:
        return True
    flag = os.environ.get("KPI_ENGINE_LOG", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def default_log_dir() -> Path:
    """KPI_ENGINE_LOG_DIR, else <cwd>/logs."""
    env = os.environ.get("KPI_ENGINE_LOG_DIR", "").strip()
    if env:
        return Path(env)
    return Path.cwd() / "logs"


def start_run(
    kind: str,
    *,
    kpi_id: Any = "unknown",
    request_id: Any | None = None,
    log_dir: str | Path | None = None,
) -> Path | None:
    """Open logs/kpi-{kind}-{kpi_id}-{timestamp}.log for this request. None if disabled."""
    if not logging_enabled(log_dir=log_dir):
        _active.set(None)
        return None
    root = Path(log_dir) if log_dir is not None else default_log_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _active.set(None)
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_kpi = _SAFE_NAME.sub("_", str(kpi_id if kpi_id is not None else "unknown"))[:80]
    seq = next(_FILE_COUNTER)
    path = root / f"kpi-{kind}-{safe_kpi}-{stamp}-{seq:04d}.log"
    logger = logging.getLogger(f"kpi_engine.run.{stamp}.{seq}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_SafeFormatter(datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    _active.set(_Run(path, logger, handler))
    info(
        "RUN start kind=%s kpi_id=%s request_id=%s log_file=%s",
        kind,
        kpi_id,
        request_id,
        path,
    )
    return path


def end_run() -> None:
    """Flush and close the current run's file handler."""
    run = _active.get()
    if run is None:
        return
    info("RUN end log_file=%s", run.path)
    run.handler.flush()
    run.handler.close()
    run.logger.removeHandler(run.handler)
    _active.set(None)


def info(msg: str, *args: Any) -> None:
    """Write an INFO line to the current run file."""
    run = _active.get()
    if run is not None:
        run.logger.info(msg, *args)


def exception(msg: str, *args: Any) -> None:
    """Write the traceback for a failed run."""
    run = _active.get()
    if run is not None:
        run.logger.exception(msg, *args)


def log_step(title: str, **fields: Any) -> None:
    """Banner for an orchestrator phase, with optional field summaries."""
    extra = " ".join(f"{k}={summarize(v)}" for k, v in fields.items())
    info("======== STEP %s ======== %s", title, extra)


def log_sql(
    sql: str,
    params: tuple[Any, ...] | list[Any] = (),
    *,
    model: Any = None,
    row_level: bool = False,
) -> None:
    """Write the DuckDB query, every parameter, and the inlined statement. Never truncated."""
    bound = render_bound_sql(sql, params)
    lines = [
        f"---------- SQL model={model} row_level={row_level} ----------",
        sql,
        f"---------- PARAMS ({len(params)}) ----------",
    ]
    for i, value in enumerate(params):
        lines.append(f"  [{i}] {_param(value)}")
    lines.append("---------- SQL BOUND (values inlined; copy into DuckDB) ----------")
    lines.append(bound)
    lines.append("---------- END SQL ----------")
    info("%s", "\n".join(lines))


def render_bound_sql(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> str:
    """Replace each `?` placeholder with a SQL literal. Log-only; never executed.

    Quoted strings and identifiers are left alone so a `?` inside a CTE comment
    or a string does not consume a parameter.
    """
    values = list(params)
    out: list[str] = []
    i = 0
    n = len(sql)
    qi = 0
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"' and j + 1 < n and sql[j + 1] == '"':
                    j += 2
                    continue
                if sql[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == "?":
            if qi < len(values):
                out.append(_sql_literal(values[qi]))
                qi += 1
            else:
                out.append("?")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def log_measure(cut: str, key: str, op: str, combo: dict[str, Any], value: Any) -> None:
    """One evaluated measure for one dimension combination."""
    info("MEASURE cut=%s key=%s op=%s combo=%s → %s", cut, key, op, summarize(combo), summarize(value))


def log_context(context: Any) -> None:
    """Write the inbound context in full (JSON). Never truncated."""
    info("---------- CONTEXT received ----------")
    if not isinstance(context, dict):
        info("%s", repr(context))
        info("---------- END CONTEXT ----------")
        return
    try:
        payload = json.dumps(context, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = repr(context)
    info("%s", payload)
    info("---------- END CONTEXT ----------")


def peek_kpi_id(context: Any) -> Any:
    """Read execution.kpi_id without adapting, for the log file name."""
    if not isinstance(context, dict):
        return "unknown"
    execution = context.get("execution")
    if isinstance(execution, dict) and execution.get("kpi_id") is not None:
        return execution["kpi_id"]
    return context.get("kpi_id") or "unknown"


def peek_request_id(context: Any) -> Any | None:
    """Read execution.request_id if the envelope has one."""
    if not isinstance(context, dict):
        return None
    execution = context.get("execution")
    if isinstance(execution, dict):
        return execution.get("request_id")
    return None


def traced(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Log invoke, duration, and a summarized return value when a run is active."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        run = _active.get()
        if run is None:
            return fn(*args, **kwargs)
        name = f"{fn.__module__}.{fn.__qualname__}"
        info("INVOKE %s %s", name, _call_args(fn, args, kwargs))
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            info("FAIL %s after %.3fs", name, time.perf_counter() - started)
            raise
        info("RETURN %s (%.3fs) → %s", name, time.perf_counter() - started, summarize(result))
        return result

    return wrapper


def summarize(value: Any, *, depth: int = 0) -> str:
    """Short, structured description of a return value. SQL strings stay whole."""
    if depth > 4:
        return "..."
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        if _looks_like_sql(value):
            return value
        if len(value) > 500:
            return repr(value[:500] + f"...<{len(value)} chars>")
        return repr(value)
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    frame = _as_frame(value)
    if frame is not None:
        return _frame_summary(frame)
    if is_dataclass(value) and not isinstance(value, type):
        parts = []
        for item in fields(value):
            raw = getattr(value, item.name)
            if item.name == "sql" and isinstance(raw, str):
                parts.append(f"sql=\n{raw}")
            elif item.name == "raw":
                parts.append("raw=<context>")
            elif item.name == "frame":
                parts.append(f"frame={summarize(raw, depth=depth + 1)}")
            elif item.name == "params":
                parts.append(f"params={summarize(raw, depth=depth + 1)}")
            else:
                parts.append(f"{item.name}={summarize(raw, depth=depth + 1)}")
        return f"{type(value).__name__}({', '.join(parts)})"
    if isinstance(value, dict):
        if len(value) > 30:
            keys = list(value.keys())[:30]
            inner = ", ".join(f"{k!r}: {summarize(value[k], depth=depth + 1)}" for k in keys)
            return "{" + inner + f", ... {len(value)} keys}}"
        inner = ", ".join(f"{k!r}: {summarize(v, depth=depth + 1)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        kind = type(value).__name__
        if seq and isinstance(seq[0], str) and _looks_like_sql(seq[0]):
            return f"{kind}[{len(seq)} sql]\n" + "\n---\n".join(str(s) for s in seq)
        shown = seq[:8]
        bits = [summarize(v, depth=depth + 1) for v in shown]
        extra = f", ... {len(seq)} items" if len(seq) > 8 else ""
        return f"{kind}[{len(seq)}]({', '.join(bits)}{extra})"
    name = type(value).__name__
    if "DuckDB" in name or "Connection" in name:
        return f"<{name}>"
    text = repr(value)
    if len(text) > 400:
        return f"<{name} {text[:400]}...>"
    return text


def _call_args(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Summarize positional/keyword arguments, skipping self/cls and connections."""
    names = []
    try:
        import inspect

        names = [p.name for p in inspect.signature(fn).parameters.values()]
    except (TypeError, ValueError):
        names = []
    bits: list[str] = []
    for i, arg in enumerate(args):
        label = names[i] if i < len(names) else f"arg{i}"
        if label in {"self", "cls", "connection", "context"} and label != "context":
            if label == "connection":
                bits.append(f"{label}=<{type(arg).__name__}>")
            continue
        if label == "context" and isinstance(arg, dict):
            bits.append("context=<see CONTEXT lines>")
            continue
        bits.append(f"{label}={summarize(arg)}")
    for key, arg in kwargs.items():
        if key == "connection":
            bits.append(f"{key}=<{type(arg).__name__}>")
            continue
        bits.append(f"{key}={summarize(arg)}")
    return " ".join(bits)


def _looks_like_sql(value: str) -> bool:
    """True when the string is a compiled SELECT/WITH that must not be truncated."""
    head = value.lstrip()[:12].upper()
    return head.startswith("SELECT") or head.startswith("WITH ") or "\nSELECT " in value.upper()


def _param(value: Any) -> str:
    """Format one bound SQL parameter in full."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return repr(value)


def _sql_literal(value: Any) -> str:
    """DuckDB literal for a bound parameter. Used only when inlining SQL for the log."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _as_frame(value: Any) -> Any | None:
    """Return value if it is a pandas DataFrame."""
    try:
        import pandas as pd
    except ImportError:
        return None
    if isinstance(value, pd.DataFrame):
        return value
    return None


def _frame_summary(frame: Any) -> str:
    """Shape, columns, and a preview of rows — not the whole extract."""
    cols = list(frame.columns)
    preview = frame.head(_PREVIEW_ROWS)
    try:
        body = preview.to_string(index=False)
    except Exception:  # noqa: BLE001 — logging must not fail the request
        body = repr(preview)
    extra = "" if len(frame) <= _PREVIEW_ROWS else f"\n  ... {len(frame) - _PREVIEW_ROWS} more rows"
    return f"DataFrame shape={frame.shape} columns={cols}\n{body}{extra}"
