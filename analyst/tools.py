"""Strict local analysis tools with one evidence record per invocation."""

from __future__ import annotations

import asyncio
import decimal
import inspect
import json
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict

from .evidence import EvidenceLedger, canonical, sha256
from .execution import PythonSession
from .memory import AnalystMemory


class ToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Literal["completed", "failed", "partial"]
    scope: dict[str, str]
    dataset_version: str | None
    policy_version: str | None
    result_reference: str
    result: dict[str, Any]
    rationale: str | None = None


def preserve_tool_envelope(output: Any, *, rationale: str | None = None) -> Any:
    """Preserve envelope fields and attach the public purpose of the tool call."""
    if isinstance(output, ToolEnvelope):
        dumped = output.model_dump()
        if rationale is not None and dumped.get("rationale") is None:
            dumped["rationale"] = rationale
        return dumped
    if isinstance(output, dict) and {"status", "scope", "result"}.issubset(output):
        envelope = dict(output)
        if rationale is not None and envelope.get("rationale") is None:
            envelope["rationale"] = rationale
        return envelope
    return output


def load_frames(path: Path) -> dict[str, pd.DataFrame]:
    extension = path.suffix.lower()
    if extension == ".csv":
        return {path.stem: pd.read_csv(path)}
    if extension in {".json", ".jsonl", ".ndjson"}:
        return {path.stem: pd.read_json(path, lines=extension != ".json")}
    if extension in {".xlsx", ".xlsm"}:
        return {
            f"{path.stem}_{sheet}": frame
            for sheet, frame in pd.read_excel(path, sheet_name=None).items()
        }
    raise ValueError("Supported input formats: CSV, JSON, JSONL and XLSX")


def function_schema(
    name: str,
    description: str,
    properties: dict,
) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            # Strict provider schemas require every property. Nullable values
            # express optional content; local callers may still omit rationale.
            "required": list(properties),
            "additionalProperties": False,
        },
    }


class AnalystTools:
    def __init__(
        self,
        run_dir: str | Path,
        input_paths: Sequence[str | Path],
        session_id: str = "",
        ledger: EvidenceLedger | None = None,
        memory: AnalystMemory | None = None,
        timeout_seconds: float | None = 60,
        memory_mb: int | None = 1024,
        memory_enabled: bool = True,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.input_paths = [Path(path).resolve() for path in input_paths]
        if any(not path.is_file() for path in self.input_paths):
            raise ValueError("All approved input paths must be existing files")
        self.sources = [
            {"path": str(path), "sha256": sha256(path.read_bytes())} for path in self.input_paths
        ]
        self.session_id = session_id
        self.timeout_seconds = timeout_seconds
        self.ledger = ledger or EvidenceLedger(self.run_dir)
        self.memory_enabled = memory_enabled
        self.memory = (memory or AnalystMemory(self.run_dir / "memory")) if memory_enabled else None
        self.python = PythonSession(
            self.run_dir, self.input_paths, timeout_seconds=timeout_seconds, memory_mb=memory_mb
        )
        self.sql = duckdb.connect(
            config={
                "enable_external_access": "false",
                "allow_unsigned_extensions": "false",
                **({"memory_limit": f"{memory_mb}MB"} if memory_mb is not None else {}),
                "threads": "1",
            }
        )
        self.tables: dict[str, pd.DataFrame] = {}
        self._sql_loaded = False
        self._lock = asyncio.Lock()
        self.todos: list[dict[str, str]] = []

    async def begin_run(
        self, run_dir: str | Path, input_paths: Sequence[str | Path] | None = None
    ) -> AnalystTools:
        """Retain session variables and curated memory; start a distinct run ledger."""
        async with self._lock:
            paths = (
                [Path(path).resolve() for path in input_paths]
                if input_paths is not None
                else self.input_paths
            )
            if any(not path.is_file() for path in paths):
                raise ValueError("All approved input paths must exist")
            self.run_dir = Path(run_dir).resolve()
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.input_paths = paths
            self.sources = [
                {"path": str(path), "sha256": sha256(path.read_bytes())} for path in paths
            ]
            self.ledger = EvidenceLedger(self.run_dir)
            self.sql.close()
            self.sql = duckdb.connect(
                config={
                    "enable_external_access": "false",
                    "allow_unsigned_extensions": "false",
                    "threads": "1",
                    **(
                        {"memory_limit": f"{self.python.memory_mb}MB"}
                        if self.python.memory_mb is not None
                        else {}
                    ),
                }
            )
            self.tables = {}
            self._sql_loaded = False
            await self.python.configure(self.run_dir, paths)
        return self

    @property
    def schemas(self) -> list[dict]:
        string = {"type": "string"}
        rationale = {
            "type": ["string", "null"],
            "description": "One concise public sentence in the selected response language explaining why this tool is chosen and which analytical hypothesis or decision question it answers. Null when no explanation helps; never include private reasoning.",
        }
        schemas = [
            function_schema(
                "local_python_repl",
                "Execute a persistent Python analysis cell. Set result to a JSON serializable aggregate. pd, np, scipy, plt, INPUT_PATHS and ARTIFACT_DIR are provided. Imports use an allowlist; files can only read approved inputs and write run artifacts. This local runner is not an OS sandbox.",
                {"code": string, "rationale": rationale},
            ),
            function_schema(
                "local_sql_engine",
                "Run read-only DuckDB SQL over approved CSV, JSON and XLSX tables. Query SELECT * FROM information_schema.tables to discover names. No external file access or COPY.",
                {"query": string, "rationale": rationale},
            ),
            function_schema(
                "dataset_profiler",
                "Profile an approved dataset: schema, missingness, uniqueness and numeric distributions, without returning raw rows.",
                {"path": string, "rationale": rationale},
            ),
            function_schema(
                "todo_planner",
                "Replace the visible work plan with concrete tasks and statuses in the selected response language. Use only for work that benefits from a visible multi-step plan; a simple question does not need one. Never include private reasoning.",
                {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    **string,
                                    "description": "A concrete user-visible task in the selected response language.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "blocked"],
                                },
                            },
                            "required": ["title", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
            ),
            function_schema(
                "memory_recall",
                "Recall curated schema, data-quality, KPI, methodology and report preferences. Offline hashed feature retrieval.",
                {"query": string},
            ),
            function_schema(
                "memory_store",
                "Store a concise reusable knowledge note, never raw records.",
                {
                    "key": string,
                    "content": string,
                    "category": {
                        "type": "string",
                        "enum": [
                            "schema",
                            "data_quality",
                            "kpi",
                            "report_preference",
                            "methodology",
                        ],
                    },
                },
            ),
            function_schema(
                "memory_update",
                "Update an existing reusable knowledge note, never raw records.",
                {
                    "key": string,
                    "content": string,
                    "category": {
                        "type": "string",
                        "enum": [
                            "schema",
                            "data_quality",
                            "kpi",
                            "report_preference",
                            "methodology",
                        ],
                    },
                },
            ),
        ]
        return (
            schemas
            if self.memory_enabled
            else [schema for schema in schemas if not schema["name"].startswith("memory_")]
        )

    def _ensure_sources_unchanged(self) -> None:
        for source in self.sources:
            if sha256(Path(source["path"]).read_bytes()) != source["sha256"]:
                raise RuntimeError(
                    "An approved source changed during the run; recomputation needs a new run"
                )

    async def call(self, name: str, arguments: dict) -> dict:
        import jsonschema

        memory = self.memory
        if name.startswith("memory_") and (not self.memory_enabled or memory is None):
            raise ValueError("Long-term memory is disabled for this workspace")
        schema = next((item for item in self.schemas if item["name"] == name), None)
        if schema is None:
            raise ValueError(f"Unknown local tool: {name}")
        if "rationale" in schema["parameters"]["properties"] and "rationale" not in arguments:
            arguments = {**arguments, "rationale": None}
        jsonschema.validate(arguments, schema["parameters"])
        started = time.perf_counter()
        async with self._lock:
            self._ensure_sources_unchanged()
            code = arguments.get(
                "code", arguments.get("query", canonical({"tool": name, "arguments": arguments}))
            )
            if name not in {"local_python_repl", "local_sql_engine"}:
                implementation: Callable[..., object] = self.call
                if name == "dataset_profiler":
                    implementation = self._profile
                elif name == "memory_recall" and memory is not None:
                    implementation = memory.recall
                elif name.startswith("memory_") and memory is not None:
                    implementation = memory.store
                code = inspect.getsource(implementation) + "\n# invocation\n" + canonical(arguments)
            result: dict[str, Any]
            try:
                if name == "local_python_repl":
                    result = await self.python.execute(arguments["code"])
                elif name == "local_sql_engine":
                    task = asyncio.create_task(asyncio.to_thread(self._query, arguments["query"]))
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(task), timeout=self.timeout_seconds
                        )
                    except TimeoutError as error:
                        self.sql.interrupt()
                        try:
                            await task
                        except Exception:
                            pass
                        raise TimeoutError("SQL execution exceeded its time limit") from error
                elif name == "dataset_profiler":
                    result = await asyncio.to_thread(self._profile, arguments["path"])
                elif name == "todo_planner":
                    self.todos = arguments["items"]
                    result = {"items": self.todos}
                elif name == "memory_recall" and memory is not None:
                    result = memory.recall(arguments["query"])
                elif name.startswith("memory_") and memory is not None:
                    memory_args = {k: v for k, v in arguments.items() if k != "rationale"}
                    result = memory.store(**memory_args, update=name == "memory_update")
                else:
                    raise ValueError(f"Unknown local tool: {name}")
                if isinstance(result, BaseModel):
                    result = result.model_dump()
                elif isinstance(result, dict) and isinstance(result.get("value"), BaseModel):
                    result["value"] = result["value"].model_dump()
                result = preserve_tool_envelope(result, rationale=arguments.get("rationale"))
                if isinstance(result, dict) and "value" in result:
                    result["value"] = preserve_tool_envelope(
                        result["value"], rationale=arguments.get("rationale")
                    )
                self._ensure_sources_unchanged()
            except Exception as error:
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            artifacts = []
            for path in (self.run_dir / "artifacts").rglob("*"):
                if path.is_file() and not any(
                    part.startswith(".")
                    for part in path.relative_to(self.run_dir / "artifacts").parts
                ):
                    artifacts.append(self.ledger.register_artifact(path))
            metadata: dict[str, Any] = {}
            if arguments.get("rationale") is not None:
                metadata["rationale"] = arguments["rationale"]
            return self.ledger.record(
                name,
                code,
                result,
                self.sources,
                elapsed_seconds=time.perf_counter() - started,
                artifacts=artifacts,
                **metadata,
            )

    def _load_sql(self) -> None:
        if self._sql_loaded:
            return
        for path in self.input_paths:
            if path.suffix.lower() not in {".csv", ".json", ".jsonl", ".ndjson", ".xlsx", ".xlsm"}:
                continue
            for name, frame in load_frames(path).items():
                safe = re.sub(r"\W", "_", name)
                if not safe or safe[0].isdigit():
                    safe = "data_" + safe
                candidate = safe
                index = 2
                while candidate in self.tables:
                    candidate = f"{safe}_{index}"
                    index += 1
                self.tables[candidate] = frame
                self.sql.register(candidate, frame)
        self._sql_loaded = True

    def _query(self, query: str) -> dict:
        self._load_sql()
        statements = self.sql.extract_statements(query)
        if len(statements) != 1 or statements[0].type.name not in {"SELECT", "EXPLAIN"}:
            raise ValueError("SQL tool accepts one read-only SELECT or EXPLAIN statement")
        if re.search(
            r"\b(read_\w+|\w+_scan|copy|attach|detach|install|load|pragma|export|import|call|httpfs|query|query_table)\b",
            query,
            re.I,
        ):
            raise ValueError(
                "SQL filesystem, extension and dynamic-query operations are unavailable"
            )
        cursor = self.sql.execute(query)
        description = cursor.description
        if description is None:
            raise ValueError("SQL statement returned no result schema")
        columns = [item[0] for item in description]
        rows = cursor.fetchmany(1001)
        if len(rows) > 1000:
            raise ValueError("SQL result exceeds 1000 rows; aggregate or LIMIT your query")
        result = {
            "columns": columns,
            "rows": [dict(zip(columns, row, strict=True)) for row in rows],
            "tables": list(self.tables),
        }
        encoded = json.dumps(
            result,
            default=lambda value: (
                float(value) if isinstance(value, decimal.Decimal) else str(value)
            ),
            allow_nan=False,
        )
        if len(encoded.encode()) > 100_000:
            raise ValueError("SQL result exceeds output limit; aggregate your query")
        return json.loads(encoded)

    def _profile(self, approved_path: str) -> dict:
        path = Path(approved_path).resolve()
        if path not in self.input_paths:
            raise ValueError("Dataset path must be one of the approved input files")
        if path.suffix.lower() in {".pdf", ".txt", ".md", ".yaml", ".yml"}:
            if path.suffix.lower() == ".pdf":
                from pypdf import PdfReader

                pages = [page.extract_text() or "" for page in PdfReader(path).pages]
                return {
                    "source_sha256": sha256(path.read_bytes()),
                    "document_type": "pdf",
                    "page_count": len(pages),
                    "text": "\n\n".join(pages)[:30000],
                    "text_truncated": sum(map(len, pages)) > 30000,
                }
            text = path.read_text(encoding="utf-8")
            return {
                "source_sha256": sha256(path.read_bytes()),
                "document_type": "text",
                "text": text[:30000],
                "text_truncated": len(text) > 30000,
            }
        profiles = {}
        for name, frame in load_frames(path).items():
            columns = []
            for column in frame.columns:
                series = frame[column]
                item = {
                    "name": str(column),
                    "dtype": str(series.dtype),
                    "missing_count": int(series.isna().sum()),
                    "distinct_count": int(series.nunique(dropna=True)),
                }
                if pd.api.types.is_numeric_dtype(series):
                    item["statistics"] = {
                        key: None if pd.isna(value) else float(value)
                        for key, value in series.describe().to_dict().items()
                    }
                columns.append(item)
            profiles[name] = {
                "row_count": len(frame),
                "column_count": len(frame.columns),
                "duplicate_row_count": int(frame.duplicated().sum()),
                "columns": columns,
            }
        return {"source_sha256": sha256(path.read_bytes()), "datasets": profiles}

    async def close(self) -> None:
        await self.python.close()
        self.sql.close()
