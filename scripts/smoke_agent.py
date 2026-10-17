"""Selectable real Workspace smoke prompts, charged to the existing rebuild ledger.

Default invocation only validates local accounting/profile and fetches actual
backend contexts. --live enables paid generation. Correctness is a separate
manual review: expected checks are never sent to the model.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import run_rebuild_eval as accounting  # noqa: E402
from analyst.config import Settings  # noqa: E402

CASES = [
    (
        "canonical_trend",
        {},
        "Phân tích xu hướng số sự kiện lỗi trong phạm vi canonical từ tháng 1 đến tháng 6 năm 2024. Nêu tổng số sự kiện, so sánh tháng 1 với tháng 6, giải thích giới hạn của mẫu số, và dẫn chứng từ dữ liệu. Không tạo báo cáo dài.",
        {
            "total_events": 1071,
            "january_events": 147,
            "june_events": 252,
            "increase": 105,
            "increase_pct": 71.42857142857143,
            "caveat": "Event counts are not defective-unit rates or causal proof.",
        },
    ),
    (
        "line3_cost",
        {"production_line": "Line-3"},
        "Với Line-3 trong phạm vi canonical tháng 1 đến tháng 6 năm 2024, cho biết số sự kiện lỗi, tổng chi phí sửa chữa đã biết, số bản ghi có và thiếu chi phí. Giải thích giới hạn của tổng chi phí này và dẫn chứng; không suy ra tiền tệ khi nguồn không xác định.",
        {
            "events": 282,
            "known_cost_events": 277,
            "unknown_cost_events": 5,
            "known_repair_cost": "189934.78",
            "caveat": "Missing costs are unknown, not zero; currency is unspecified.",
        },
    ),
    (
        "inspection_causality",
        {},
        "Có thể kết luận Automated Testing hiệu quả hơn Visual Inspection và nên thay toàn bộ kiểm tra trực quan không? Hãy dùng dữ liệu canonical để giải thích điều có thể và không thể kết luận, rồi nêu dữ liệu bổ sung cần thu thập.",
        {
            "Automated Testing": 325,
            "Visual Inspection": 372,
            "Manual Testing": 374,
            "caveat": "Must reject causal effectiveness/replacement conclusion; require inspection exposure, comparable product/risk mix, ground truth and false negatives.",
        },
    ),
]

DEFAULT_CASE_NAMES = tuple(case[0] for case in CASES)
CASES.extend(
    [
        (
            "june_denominator",
            {"start_month": "2024-06", "end_month": "2024-06"},
            "Trong phạm vi tháng 6 năm 2024 đang chọn, giải thích cách tính số sự kiện trên 1.000 sản lượng tham chiếu: tổng sự kiện, tử số thực sự dùng, mẫu số và kết quả. Giải thích mọi chênh lệch giữa tổng sự kiện với tử số, và vì sao chỉ số này không phải tỷ lệ sản phẩm lỗi. Dẫn chứng từ dữ liệu.",
            {
                "total_events": 252,
                "linked_events": 251,
                "unmatched_events": 1,
                "reference_output": 99060,
                "reference_intensity_per_1000": 2.533817888148597,
                "caveat": "Use linked events over reference output; static reference output is not actual inspected or produced units, and events are not distinct defective units.",
            },
        ),
        (
            "empty_cohort",
            {"production_line": "Line-1", "category": "Category 2"},
            "Phân tích phạm vi Line-1, Category 2 đang chọn từ tháng 1 đến tháng 6 năm 2024. Không thấy sự kiện lỗi có chứng minh chất lượng hoàn hảo không? Cho biết số sản phẩm trong tập tham chiếu, mẫu số, cường độ sự kiện và độ phủ chi phí có tính được không; đề xuất bước kiểm tra tiếp theo và giữ đúng phạm vi đang chọn.",
            {
                "events": 0,
                "cohort_products": 0,
                "reference_output": None,
                "reference_intensity_per_1000": None,
                "cost_coverage_pct": None,
                "caveat": "Empty cohort does not establish perfect quality; preserve filters, distinguish unavailable ratios from zero, and verify scope/master mapping before broadening scope.",
            },
        ),
    ]
)


def select_cases(names=None):
    """Keep the original baseline by default and cap every batch at $0.90."""
    names = list(DEFAULT_CASE_NAMES if names is None else names)
    if not 1 <= len(names) <= 3:
        raise ValueError("Select one to three cases per batch (maximum $0.90 reservation)")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate case names are not allowed")
    by_name = {case[0]: case for case in CASES}
    unknown = set(names) - by_name.keys()
    if unknown:
        raise ValueError("Unknown cases: " + ", ".join(sorted(unknown)))
    return [by_name[name] for name in names]


def progress(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=True), flush=True)


def budget_gate():
    if not (accounting.STATE / "task_spend.jsonl").is_file():
        raise RuntimeError("Existing aggregate ledger is required; never create a fresh budget")
    cost = accounting.task_cost()
    if cost["unresolved_reserved_usd"] or cost["unknown_usage_responses"]:
        raise RuntimeError("Aggregate ledger has unresolved requests; no model dispatch")
    if not math.isfinite(cost["conservative_accounted_usd"]):
        raise RuntimeError("Aggregate cost is not finite")
    if cost["conservative_accounted_usd"] + 0.30 > 20:
        raise RuntimeError("Existing task allocation has insufficient unreserved balance")
    return cost


async def load_app_factory(settings, output):
    # The convenience module-level app would otherwise open historical SQLite.
    # Redirect only import-time configuration, then restore immediately. The
    # executing Workspace below receives actual credentials and production code.
    from unittest.mock import patch

    bootstrap = replace(settings, api_key="", mode="auto", state_dir=str(output / "bootstrap"))
    if "analyst.app" in sys.modules:
        raise RuntimeError("Run smoke in a fresh process; analyst.app already imported")
    with patch.object(Settings, "from_env", return_value=bootstrap):
        module = importlib.import_module("analyst.app")
    await module.app.state.workspace.close()
    return module.create_app


def needs_reservation(rid: str | None, reserved: set[str]) -> bool:
    """Session-level messages have no run and must never reserve money."""
    return isinstance(rid, str) and bool(rid) and rid not in reserved


def save_session_evidence(workspace: Any, output: Path, cases: list[dict[str, Any]]) -> None:
    """Retain failure evidence even when HTTP submission or task awaiting raises."""
    for outcome in cases:
        sid = outcome.get("session_id")
        if not sid:
            continue
        folder = output / outcome["case"]
        session = workspace.store.session(sid)
        accounting.write_json(folder / "session.json", session)
        runs = session.get("runs", [])
        if runs:
            rid = runs[-1]["id"]
            row = workspace.store.db.execute(
                "SELECT result,state FROM runs WHERE id=?", (rid,)
            ).fetchone()
            result = json.loads(row["result"]) if row["result"] else {}
            events = [e for e in session["events"] if e.get("run_id") == rid]
            outcome["run_id"] = rid
            outcome["persisted_state"] = row["state"]
        else:
            result, events = {}, session.get("events", [])
        usage = [e["data"] for e in events if e["type"] == "usage"]
        report = [
            e["data"]
            for e in events
            if e["type"] == "message" and e["data"].get("role") == "assistant"
        ]
        outcome["provider_event_counts"] = {
            kind: sum(e["type"] == kind for e in events)
            for kind in ("model_started", "response_created", "usage")
        }
        for filename, value in (("result", result), ("usage", usage), ("report", report)):
            accounting.write_json(folder / (filename + ".json"), value)


def exact_usage(row):
    return (
        accounting.accountable_usage(row)
        and row.get("cost_basis") == "provider_usage"
        and isinstance(row.get("cost_usd"), (int, float))
        and math.isfinite(row["cost_usd"])
        and all(type(row.get(k)) is int for k in ("cached_tokens", "cache_write_tokens"))
        and row.get("hosted_tool_fees_usd", 0) == 0
    )


async def run(live, case_names=None):
    import httpx
    from agents import set_tracing_disabled

    selected_cases = select_cases(case_names)
    base = Settings.from_env(ROOT)
    if (base.model, base.reasoning_effort, base.run_timeout_seconds, base.max_output_tokens) != (
        "gpt-6-luna",
        "medium",
        450,
        12000,
    ):
        raise RuntimeError("Compiled application profile differs from 450s/medium/12000")
    if any(
        getattr(base, k)
        for k in (
            "enable_file_search",
            "enable_code_interpreter",
            "enable_web_search",
            "enable_mcp",
            "enable_tool_search",
            "multi_agent_native",
        )
    ):
        raise RuntimeError("Hosted tools or native multiagent are enabled")
    if live and not base.api_key:
        raise RuntimeError("Live smoke requires configured API key")
    before = budget_gate()
    if before["conservative_accounted_usd"] + 0.30 * len(selected_cases) > 20:
        raise RuntimeError("Existing allocation cannot cover selected case reservations")
    protocol = accounting.read_json(accounting.PROTOCOL)
    oracle = accounting.read_json(accounting.ORACLE)
    if protocol["resource_limits"]["task_usd"] != 20:
        raise RuntimeError("Existing task allocation changed; review harness")
    attempt = "smoke-" + uuid.uuid4().hex
    output = accounting.STATE / "smoke" / attempt
    output.mkdir(parents=True, exist_ok=False)
    # This local ceiling is subordinate to the unchanged shared task ledger.
    settings = replace(
        base,
        state_dir=str(output / "workspace"),
        dev_api_budget_usd=0.90,
        max_cost_usd_per_run=0.30,
        mode="live" if live else "auto",
    )
    settings.validate()
    set_tracing_disabled(True)
    factory = await load_app_factory(settings, output)
    app = factory(settings)
    workspace = app.state.workspace
    frozen = accounting.manifest()
    active: dict[str, str] = {}
    reserved: set[str] = set()
    emitted_usage = {}
    original_emit = workspace.emit

    async def observe(sid, rid, kind, data):
        # The first status event precedes any model or local preparation await.
        if live and needs_reservation(rid, reserved):
            budget_gate()
            if len(reserved) >= len(selected_cases):
                raise RuntimeError("Smoke dispatch count exceeded")
            accounting.append_spend(
                {
                    "kind": "turn_reservation",
                    "suite_id": attempt,
                    "case_id": active["name"],
                    "run_id": rid,
                    "amount_usd": 0.30,
                }
            )
            reserved.add(rid)
        result = await original_emit(sid, rid, kind, data)
        if kind == "usage" and data.get("response_id"):
            key = data["response_id"]
            if key not in emitted_usage:
                accounting.append_spend(
                    {
                        "kind": "response_usage",
                        "suite_id": attempt,
                        "case_id": active["name"],
                        "session_id": sid,
                        "run_id": rid,
                        **data,
                        "independent_price_usd": accounting.independent_price(data, protocol),
                    }
                )
                emitted_usage[key] = data
        return result

    if live:
        workspace.emit = observe
    summary = {
        "attempt": attempt,
        "live": live,
        "selected_cases": [case[0] for case in selected_cases],
        "cost_before": before,
        "profile": {
            "model": base.model,
            "effort": base.reasoning_effort,
            "application_seconds": 450,
            "output_tokens": 12000,
            "outer_seconds": 600,
            "per_prompt_usd": 0.30,
            "attempt_limit_usd": 0.90,
            "aggregate_limit_usd": 20,
        },
        "oracle_sha256": accounting.sha(accounting.ORACLE.read_bytes()),
        "application_manifest": frozen,
        "cases": [],
        "stop_reason": None,
        "correctness_review": "Not run; independent manual review required",
        "harness_sha256": accounting.sha(Path(__file__).read_bytes()),
        "ledger_sha256_before": accounting.sha(
            (accounting.STATE / "task_spend.jsonl").read_bytes()
        ),
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            for name, filters, prompt, checks in selected_cases:
                active["name"] = name
                if accounting.manifest() != frozen:
                    raise RuntimeError("Application source changed during smoke")
                budget_gate()
                response = await client.get("/api/dashboard", params=filters)
                response.raise_for_status()
                context = response.json()
                if (
                    context["context"]["source_sha256"] != oracle["source"]["sha256"]
                    or context["context"]["master_sha256"] != oracle["master_sha256"]
                ):
                    raise RuntimeError("Actual backend source/master differs from oracle")
                folder = output / name
                accounting.write_json(folder / "context.json", context)
                accounting.write_json(folder / "expected-review.json", checks)
                request = {
                    "text": prompt,
                    "analysis_context_id": context["context"]["context_id"],
                    "language": "vi",
                    "action_intent": "none",
                }
                accounting.write_json(folder / "request.json", request)
                outcome: dict[str, Any] = {
                    "case": name,
                    "generation": "Not run",
                    "correctness": "Not run",
                }
                summary["cases"].append(outcome)
                if not live:
                    progress("preflight_context_verified", case=name)
                    continue
                sid_response = await client.post("/api/sessions")
                sid_response.raise_for_status()
                sid = sid_response.json()["id"]
                outcome["session_id"] = sid
                accounting.write_json(output / "summary.json", summary)
                progress("prompt_started", case=name)
                started = time.monotonic()
                response = await client.post(f"/api/sessions/{sid}/prompt", json=request)
                response.raise_for_status()
                await asyncio.sleep(0)
                task = workspace.tasks[sid]
                timed_out = False
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=600)
                except TimeoutError:
                    timed_out = True
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                session = workspace.store.session(sid)
                rid = session["runs"][-1]["id"]
                row = workspace.store.db.execute(
                    "SELECT result,state FROM runs WHERE id=?", (rid,)
                ).fetchone()
                result = json.loads(row["result"]) if row["result"] else {}
                events = [e for e in session["events"] if e.get("run_id") == rid]
                usage = {
                    e["data"]["response_id"]: e["data"]
                    for e in events
                    if e["type"] == "usage" and e["data"].get("response_id")
                }
                starts = sum(e["type"] == "model_started" for e in events)
                created = {
                    e["data"]["response_id"]
                    for e in events
                    if e["type"] == "response_created" and e["data"].get("response_id")
                }
                known = (
                    bool(usage)
                    and starts == len(usage)
                    and created <= usage.keys()
                    and all(exact_usage(x) for x in usage.values())
                )
                if known:
                    accounting.append_spend(
                        {
                            "kind": "turn_settlement",
                            "suite_id": attempt,
                            "run_id": rid,
                            "exact_cost_usd": sum(x["cost_usd"] for x in usage.values()),
                            "accounted_cost_upper_bound_usd": sum(
                                x.get("cost_upper_bound_usd", x["cost_usd"]) for x in usage.values()
                            ),
                        }
                    )
                for filename, value in (
                    ("result", result),
                    ("session", session),
                    ("usage", list(usage.values())),
                ):
                    accounting.write_json(folder / (filename + ".json"), value)
                report = [
                    e["data"]
                    for e in events
                    if e["type"] == "message" and e["data"].get("role") == "assistant"
                ]
                accounting.write_json(folder / "report.json", report)
                outcome.update(
                    generation=row["state"],
                    session_id=sid,
                    run_id=rid,
                    elapsed_seconds=time.monotonic() - started,
                    usage_complete=known,
                    cost_usd=sum(x.get("cost_usd", 0) or 0 for x in usage.values()),
                )
                progress("prompt_finished", case=name, state=row["state"], usage_complete=known)
                if timed_out or row["state"] in {"timeout", "provider_stopped"} or not known:
                    summary["stop_reason"] = (
                        "Timeout, provider stop, or unresolved usage; no subsequent dispatch"
                    )
                    break
                accounting.write_json(output / "summary.json", summary)
    except BaseException as error:
        summary["stop_reason"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        # Stop outstanding work before reading durable evidence, without changing
        # historical outcomes or declaring uncertain provider usage settled.
        pending = [task for task in workspace.tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            save_session_evidence(workspace, output, summary["cases"])
        finally:
            await workspace.close()
        summary["cost_after"] = accounting.task_cost()
        accounting.write_json(output / "summary.json", summary)
        progress(
            "evidence_saved", output=str(output), live=live, stopped=bool(summary["stop_reason"])
        )
    return 1 if summary["stop_reason"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run selected paid prompts within existing aggregate allocation",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=[case[0] for case in CASES],
        help="Select one to three cases in execution order; default is the original three",
    )
    parser.add_argument("--list-cases", action="store_true", help="List cases without preflight")
    args = parser.parse_args()
    if args.list_cases:
        for name, _, _, _ in CASES:
            print(name + (" (default)" if name in DEFAULT_CASE_NAMES else ""))
        return 0
    try:
        select_cases(args.cases)
    except ValueError as error:
        parser.error(str(error))
    return asyncio.run(run(args.live, args.cases))


if __name__ == "__main__":
    raise SystemExit(main())
