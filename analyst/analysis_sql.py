"""Read-only SQL analysis of verified, immutable Dashboard context inputs."""

from __future__ import annotations

import asyncio
import datetime
import decimal
import io
import json
import re
import threading
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd

from .evidence import sha256


def _json_value(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    raise TypeError(f"SQL result has an unsupported JSON value: {type(value).__name__}")


def _input_tables(connection: duckdb.DuckDBPyConnection, query: str) -> set[str]:
    """Read actual base-table nodes without binding absent schemas or aliases."""
    tree = json.loads(
        cast(
            tuple[Any, ...], connection.execute("SELECT json_serialize_sql(?)", [query]).fetchone()
        )[0]
    )
    if tree.get("error"):
        raise ValueError("Snapshot SQL could not parse its source tables")
    tables: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            if value.get("class") == "FUNCTION" and re.fullmatch(
                r"read_\w+|\w+_scan|copy|attach|detach|install|load|pragma|export|import|call|httpfs|query|query_table",
                value.get("function_name", ""),
                re.I,
            ):
                raise ValueError("Snapshot SQL cannot access files, extensions or dynamic queries")
            if value.get("type") == "BASE_TABLE":
                tables.add(value["table_name"].lower())
            for entry in value.get("cte_map", {}).get("map", []):
                if entry["key"].lower() in {"events", "products"}:
                    raise ValueError(
                        "Snapshot SQL cannot shadow the canonical events or products tables"
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(tree)
    return tables


def query_snapshot(
    query: str,
    paths: list[Path],
    expected_hashes: dict[str, str],
    *,
    cancellation: threading.Event | None = None,
    active: list[Any] | None = None,
) -> dict[str, Any]:
    """Execute one SELECT over in-memory copies whose bytes match the receipt.

    The connection cannot read files, load extensions, or modify source inputs.
    Full results are retained locally; the Agent's standard receipt projection
    handles presentation and navigation without truncating this computation.
    """
    connection = duckdb.connect(
        config={"enable_external_access": "false", "allow_unsigned_extensions": "false"}
    )
    if active is not None:
        active.append(connection)
    try:
        statements = connection.extract_statements(query)
        if len(statements) != 1 or statements[0].type.name != "SELECT":
            raise ValueError("Snapshot SQL accepts exactly one read-only SELECT statement")
        tables = {}
        aliases = {"cleaned_records.csv": "events", "reference_products.csv": "products"}
        referenced_tables = _input_tables(connection, query)
        if not referenced_tables & set(aliases.values()):
            raise ValueError("Snapshot SQL must read events or products from the selected context")
        if {path.name for path in paths} != set(aliases):
            raise ValueError("Snapshot SQL requires the canonical event and product inputs")
        for path in paths:
            content = path.read_bytes()
            if sha256(content) != expected_hashes.get(path.name):
                raise ValueError("Snapshot SQL input does not match its canonical receipt")
            frame = pd.read_csv(io.BytesIO(content))
            name = aliases[path.name]
            connection.register(name, frame)
            tables[name] = {
                "columns": list(frame.columns),
                "input_name": path.name,
                "sha256": sha256(content),
            }
        if cancellation is not None and cancellation.is_set():
            raise InterruptedError("Snapshot SQL was cancelled before execution")
        cursor = connection.execute(query)
        columns = [item[0] for item in cursor.description]
        if len(set(columns)) != len(columns):
            raise ValueError("Snapshot SQL result columns need unique aliases")
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        return json.loads(
            json.dumps(
                {"columns": columns, "rows": rows, "tables": tables},
                default=_json_value,
                allow_nan=False,
            )
        )
    finally:
        connection.close()


async def execute_snapshot_query(
    query: str, paths: list[Path], expected_hashes: dict[str, str]
) -> dict[str, Any]:
    """Interrupt active DuckDB work when the owner cancels a run."""
    cancellation = threading.Event()
    active: list[Any] = []
    task = asyncio.create_task(
        asyncio.to_thread(
            query_snapshot,
            query,
            paths,
            expected_hashes,
            cancellation=cancellation,
            active=active,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancellation.set()
        for connection in active:
            try:
                connection.interrupt()
            except duckdb.Error:
                pass
        try:
            await task
        except Exception:
            pass
        raise
