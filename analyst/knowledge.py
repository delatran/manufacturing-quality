"""Curated PDF rules in local memory and an expiring hosted retrieval index.

Only the two explicitly named knowledge documents are eligible for upload. The
source datasets and the independent numerical oracle never enter this index.
API contracts: https://developers.openai.com/api/docs/guides/retrieval and
https://developers.openai.com/api/reference/python/resources/files/methods/create
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

from openai import AsyncOpenAI

from analyst.config import Settings, model_capabilities
from analyst.memory import AnalystMemory

DOCUMENTS = ("knowledge/kpi_registry.yaml", "knowledge/case_brief.md")
STORAGE_USD_PER_GB_DAY = 0.10
STORAGE_PRICING_SOURCE = "https://developers.openai.com/api/docs/guides/retrieval#pricing"
_locks: dict[str, asyncio.Lock] = {}
KnowledgeEmitter = Callable[[str, dict[str, Any]], Awaitable[object] | object]


class _PollingOptions(TypedDict, total=False):
    max_wait_seconds: float


def _documents(root: Path) -> dict[str, bytes]:
    # Registry is JSON-compatible YAML. Convert to Markdown because file_search
    # supports Markdown, whereas YAML is absent from its documented file types.
    registry = json.loads((root / DOCUMENTS[0]).read_text(encoding="utf-8"))
    registry_text = (
        "# Case business rules and KPI registry\n\n```json\n"
        + json.dumps(registry, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    return {
        "kpi_registry.md": registry_text.encode("utf-8"),
        "case_brief.md": (root / DOCUMENTS[1]).read_bytes(),
    }


def knowledge_fingerprint(root: Path | str) -> str:
    """Stable prompt-cache identity, invalidated when curated source bytes change."""
    digest = hashlib.sha256()
    for name in DOCUMENTS:
        payload = (Path(root) / name).read_bytes()
        digest.update(name.encode() + b"\0" + hashlib.sha256(payload).digest())
    return digest.hexdigest()


def seed_memory(root: Path, memory: AnalystMemory) -> int:
    """Persist definitions and handling rules, never observations or raw rows."""
    registry = json.loads((root / DOCUMENTS[0]).read_text(encoding="utf-8"))
    notes = [
        (
            "case.source",
            "methodology",
            "Case requirements source: "
            + registry["source"]["file"]
            + ". SHA256 "
            + registry["source"]["sha256"]
            + ". "
            + registry["source"]["note"],
        )
    ]
    for key, content in registry.get("adopted_assumptions", {}).items():
        notes.append(
            (
                "case.rule." + key,
                "data_quality"
                if key in {"dates", "deduplication", "master_join", "cost"}
                else "methodology",
                content,
            )
        )
    for key, item in registry.get("kpis", {}).items():
        notes.append(
            (
                "case.kpi." + key,
                "kpi",
                key + ": " + "; ".join(f"{field}: {value}" for field, value in item.items()),
            )
        )
    for item in registry.get("pdf_requirements", []):
        if "fields" in item:
            notes.append(
                (
                    "case.schema." + item["id"],
                    "schema",
                    item["id"] + " fields: " + ", ".join(item["fields"]),
                )
            )
    for key, category, content in notes:
        try:
            memory.store(key, content, category)
        except ValueError as error:
            if str(error) != "Memory key already exists; use memory_update":
                raise
            memory.store(key, content, category, update=True)
    return len(notes)


async def _emit(emit: KnowledgeEmitter | None, status: dict[str, Any]) -> None:
    if emit:
        result = emit("memory", {"operation": "knowledge_prepare", "result": status})
        if inspect.isawaitable(result):
            await result


async def prepare_knowledge(
    settings: Settings, memory: AnalystMemory | None, emit: KnowledgeEmitter | None = None
) -> tuple[Settings, dict[str, Any]]:
    """Seed local definitions and prepare retrieval only for an actual live run.

    An explicit external store is used as configured and is never modified. An
    app-owned store is reused only after remote status and metadata readback.
    Failed ingestion remains observable and cannot masquerade as synchronization.
    """
    root = Path(settings.root)
    fingerprint = knowledge_fingerprint(root)
    suffix = f":knowledge-{fingerprint[:16]}"
    effective = replace(
        settings,
        prompt_cache_key=settings.prompt_cache_key
        if settings.prompt_cache_key.endswith(suffix)
        else settings.prompt_cache_key + suffix,
    )
    status: dict[str, Any] = {
        "fingerprint": fingerprint,
        "documents": list(DOCUMENTS),
        "memory_notes": seed_memory(root, memory) if settings.memory_enabled and memory else 0,
        "vector_store_id": settings.vector_store_id or None,
        "remote_verified": False,
    }

    async def finish(state: str) -> tuple[Settings, dict[str, Any]]:
        status["status"] = state
        await _emit(emit, status)
        return effective, status

    if not settings.enable_file_search:
        return await finish("disabled")
    if not settings.api_key:
        return await finish("pending_no_api_key")
    if not settings.live:
        return await finish("pending_mock_mode")
    if "file_search" not in model_capabilities(settings.model)["tools"]:
        return await finish("unsupported_by_model")
    if settings.vector_store_id:
        return await finish("configured_external_store")

    documents = _documents(root)
    total_bytes = sum(map(len, documents.values()))
    max_bytes = settings.run_limit("knowledge_upload_max_bytes")
    days = getattr(settings, "knowledge_vector_expiry_days", 1)
    finite_budgets = [
        limit
        for name in ("max_cost_usd_per_run", "dev_api_budget_usd")
        if (limit := settings.run_limit(name)) is not None
    ]
    budget = min(finite_budgets) if finite_budgets else None
    tool_timeout = settings.run_limit("tool_timeout_seconds")
    status.update(
        {
            "upload_bytes": total_bytes,
            "expiry_days": days,
            "storage_pricing_source": STORAGE_PRICING_SOURCE,
            "storage_rate_usd_per_gb_day": STORAGE_USD_PER_GB_DAY,
        }
    )
    if (max_bytes is not None and total_bytes > max_bytes) or not 1 <= days <= 30:
        return await finish("pending_upload_limit")
    if budget is not None and budget <= 0:
        return await finish("pending_budget")

    directory = (root / settings.state_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "knowledge_vector_state.json"
    lock = _locks.setdefault(str(state_path), asyncio.Lock())
    async with lock:
        state: dict[str, Any] = {}
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        async with AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.openai_base_url or "https://api.openai.com/v1",
            timeout=tool_timeout,
        ) as client:
            store = None
            if state.get("fingerprint") == fingerprint and state.get("vector_store_id"):
                try:
                    candidate = await client.vector_stores.retrieve(state["vector_store_id"])
                    if (
                        candidate.status == "completed"
                        and (candidate.metadata or {}).get("knowledge_fingerprint") == fingerprint
                        and candidate.file_counts.completed == len(documents)
                        and not candidate.file_counts.failed
                        and not candidate.file_counts.in_progress
                    ):
                        store = candidate
                except Exception:
                    # A stale/deleted index is rebuilt. Credential/network failures
                    # will still be reported by the create attempt below.
                    pass
            if store is None:
                file_ids: list[str] = []
                created_store_id = None
                try:
                    store = await client.vector_stores.create(
                        name="Analyst curated case knowledge",
                        metadata={"knowledge_fingerprint": fingerprint},
                        expires_after={"anchor": "last_active_at", "days": days},
                    )
                    created_store_id = store.id
                    for name, data in documents.items():
                        uploaded = await client.files.create(
                            file=(name, data, "text/markdown"),
                            purpose="assistants",
                            expires_after={"anchor": "created_at", "seconds": days * 86400},
                        )
                        file_ids.append(uploaded.id)
                        polling: _PollingOptions = (
                            {"max_wait_seconds": tool_timeout} if tool_timeout is not None else {}
                        )
                        indexed = await client.vector_stores.files.create_and_poll(
                            uploaded.id,
                            vector_store_id=store.id,
                            attributes={"knowledge_fingerprint": fingerprint, "curated": True},
                            **polling,
                        )
                        if indexed.status != "completed":
                            raise RuntimeError("knowledge_ingestion_incomplete")
                    store = await client.vector_stores.retrieve(store.id)
                    if store.status != "completed" or store.file_counts.completed != len(documents):
                        raise RuntimeError("knowledge_readback_incomplete")
                    # Billable size is parsed chunks + embeddings, not upload size.
                    # Ignore the account-wide free GB for a conservative reservation.
                    reservation = store.usage_bytes / 1_000_000_000 * STORAGE_USD_PER_GB_DAY * days
                    if budget is not None and reservation > budget:
                        raise RuntimeError("knowledge_storage_budget_exceeded")
                    state = {
                        "fingerprint": fingerprint,
                        "vector_store_id": store.id,
                        "file_ids": file_ids,
                        "usage_bytes": store.usage_bytes,
                        "storage_reservation_usd": reservation,
                    }
                    temporary = state_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    temporary.replace(state_path)
                except Exception as error:
                    # Delete only resources created by this attempt, never an
                    # explicit external store or the previous successful index.
                    cleanup_failed = False
                    if created_store_id:
                        try:
                            await client.vector_stores.delete(created_store_id)
                        except Exception:
                            cleanup_failed = True
                    for file_id in file_ids:
                        try:
                            await client.files.delete(file_id)
                        except Exception:
                            cleanup_failed = True
                    status.update(
                        {"error_type": type(error).__name__, "cleanup_failed": cleanup_failed}
                    )
                    if isinstance(error, RuntimeError) and str(error).startswith("knowledge_"):
                        status["reason"] = str(error)
                    return await finish("pending_remote_error")
            effective = replace(effective, vector_store_id=store.id)
            status.update(
                {
                    "vector_store_id": store.id,
                    "remote_verified": True,
                    "usage_bytes": store.usage_bytes,
                    "storage_reservation_usd": store.usage_bytes
                    / 1_000_000_000
                    * STORAGE_USD_PER_GB_DAY
                    * days,
                }
            )
            return await finish("synchronized")
