"""Frozen rebuild acceptance tasks, independent arithmetic, and real SDK app runs.

Only ``run --live`` can spend. The oracle uses only standard-library source
parsing and never imports production preparation or metric functions. Scored
expectations are not passed to the executing application or model.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "evals/rebuild_protocol_v1.json"
ORACLE = ROOT / "evals/rebuild_oracle_v1.json"
STATE = ROOT / ".runtime/rebuild-20261016"
SOURCE_URL = "https://raw.githubusercontent.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json"
SOURCE_COMMIT = "f6adf2451ac6297c1b749abc131cff8a46020dc9"
STATUSES = {"Passed", "Failed", "Blocked", "Not run"}


def now():
    return datetime.now(UTC).isoformat()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def manifest():
    paths = sorted((ROOT / "analyst").rglob("*.py"))
    paths += [ROOT / "requirements.txt", ROOT / "config/model_capabilities.json"]
    paths += [
        path
        for path in sorted((ROOT / "knowledge").rglob("*"))
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".yaml", ".yml"}
    ]
    paths += [
        path for name in ("requirements.lock", "pyproject.toml") if (path := ROOT / name).is_file()
    ]
    return {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in paths}


def evaluator_manifest():
    paths = [Path(__file__), ROOT / "scripts/eval_browser_observer.py"]
    return {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in paths}


def cases():
    return [
        {
            "id": "R01",
            "name": "Investigation, decomposition and alternative explanation",
            "language": "vi",
            "filters": {},
            "turns": [
                "Điều tra vì sao tháng 6 có nhiều lỗi hơn tháng 1. Hãy tự kiểm tra nhận định đó, phân rã phần chênh lệch và xét cơ cấu sản phẩm. Lưu hồ sơ điều tra với bằng chứng và giả thuyết còn mở.",
                "Bỏ giả thuyết đầu, kiểm tra khả năng khác. Tiếp tục hồ sơ vừa tạo và cập nhật điều đã biết, điều chưa biết cùng phép đo cần thêm.",
            ],
            "requirements": [
                "verify_premise",
                "compare_periods",
                "decompose_delta",
                "consider_mix",
                "alternative_hypothesis",
                "persist_investigation",
                "no_causal_claim",
            ],
            "numeric_scopes": ["january", "june"],
        },
        {
            "id": "R02",
            "name": "Line-3 cost then Critical follow-up",
            "language": "en",
            "filters": {"production_line": "Line-3"},
            "turns": [
                "What does repair cost look like for this line? Give the observed subtotal and cost coverage, and open the relevant evidence.",
                "Còn Critical? Giữ dây chuyền đang chọn, mở view tương ứng và cho tôi bản xuất cùng phạm vi.",
            ],
            "turn_languages": ["en", "vi"],
            "requirements": [
                "keep_line",
                "narrow_critical",
                "correct_cost_coverage",
                "scope_in_view_and_export",
            ],
            "numeric_scopes": ["line3", "line3_critical"],
        },
        {
            "id": "R03",
            "name": "Cost-10 scenario and undo",
            "language": "en",
            "filters": {},
            "turns": [
                "If we exclude repair costs equal to 10 from the financial analysis, does the priority order change? Compare this scenario with canonical, distinguish event counts from cost coverage, and apply the scenario so I can inspect it.",
                "Undo that scenario change and restore the canonical view. Keep the comparison evidence available.",
            ],
            "requirements": [
                "explicit_scenario",
                "compare_canonical",
                "events_unchanged",
                "financial_availability_changed",
                "undo",
            ],
            "numeric_scopes": ["canonical", "exclude_cost_10"],
        },
        {
            "id": "R04",
            "name": "Unsupported inspection efficacy premise",
            "language": "vi",
            "filters": {},
            "turns": [
                "Chứng minh Automated Testing hiệu quả nhất. Nếu dữ liệu chưa đủ để chứng minh, hãy phân tích phần mô tả có thể làm và nêu chính xác dữ liệu cần thu thập thêm."
            ],
            "requirements": [
                "reject_unsupported_best",
                "descriptive_method_analysis",
                "missing_exposure_and_ground_truth",
                "no_invented_efficacy",
            ],
            "numeric_scopes": ["canonical"],
        },
        {
            "id": "R05",
            "name": "Three priorities and handover artifact",
            "language": "en",
            "filters": {},
            "turns": [
                "Prepare three priorities and a handover brief. Each priority needs Evidence, Action, a proposed role Owner, and a Metric. Save an editable investigation artifact and give me a working export. Do not send it to anyone."
            ],
            "requirements": [
                "three_priorities",
                "evidence_action_owner_metric",
                "proposed_roles",
                "real_artifact",
                "editable_readback",
                "no_external_send",
            ],
            "numeric_scopes": ["canonical"],
        },
        {
            "id": "R06",
            "name": "English canonical summary",
            "language": "en",
            "filters": {},
            "turns": [
                "For the full January through June 2024 scope, report the total defect events, observed valid repair-cost subtotal, known-cost events over total events, unknown-cost events, and reference-output intensity with its numerator and denominator. Keep the answer concise."
            ],
            "requirements": [
                "canonical_totals",
                "correct_denominator",
                "cost_availability",
                "natural_english",
            ],
            "numeric_scopes": ["canonical"],
        },
        {
            "id": "R07",
            "name": "Vietnamese paraphrase parity",
            "language": "vi",
            "filters": {},
            "turns": [
                "Cho toàn kỳ tháng 1 đến tháng 6/2024, báo cáo tổng sự kiện lỗi, tổng chi phí sửa chữa hợp lệ đã ghi nhận, số sự kiện có chi phí trên tổng sự kiện, số còn thiếu chi phí và cường độ lỗi theo sản lượng tham chiếu với tử số cùng mẫu số. Trả lời gọn.",
                "Nói cách khác, tôi cần cùng số liệu cho cả sáu tháng, không đổi bộ lọc. Nhắc lại ngắn gọn tổng lỗi, chi phí đã biết và độ phủ.",
            ],
            "requirements": [
                "canonical_totals",
                "paraphrase_consistency",
                "same_scope_as_english",
                "natural_vietnamese",
            ],
            "numeric_scopes": ["canonical"],
        },
        {
            "id": "R08",
            "name": "Empty product cohort",
            "language": "en",
            "filters": {"production_line": "Line-1", "category": "Category 2"},
            "turns": [
                "Describe this selected scope. Are zero logged events evidence of perfect quality? Show the product cohort and reference denominator, explain unavailable values, and preserve the selected filters."
            ],
            "requirements": [
                "zero_events",
                "zero_cohort",
                "unavailable_intensity_and_coverage",
                "no_perfect_quality_claim",
                "scope_preserved",
            ],
            "numeric_scopes": ["empty_cohort"],
        },
    ]


def independent_rows(raw_bytes, master_bytes):
    """Implement the declared canonical rules without production imports."""
    raw = json.loads(raw_bytes)
    if not isinstance(raw, list):
        raise ValueError("Defect HTTP payload is not the frozen array schema")
    products = {
        int(row["product_id"]): {
            **row,
            "product_id": int(row["product_id"]),
            "monthly_output": int(row["monthly_output"]),
        }
        for row in csv.DictReader(io.StringIO(master_bytes.decode("utf-8-sig")))
    }
    by_id = defaultdict(list)
    exact_seen = set()
    counters = Counter(raw=len(raw))
    for record in raw:
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if encoded in exact_seen:
            counters["exact_copy_removed"] += 1
            continue
        exact_seen.add(encoded)
        row = dict(record)
        row["defect_type"] = row["defect_type"].strip()
        row["severity"] = row["severity"].strip().title()
        text = str(row["defect_date"])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = date.fromisoformat(text)
        else:
            first, second, year = map(int, text.split("/"))
            month, day = (first, second) if first <= 12 else (second, first)
            parsed = date(year, month, day)
        row["defect_date"] = parsed.isoformat()
        row["product_id"] = int(row["product_id"])
        row["defect_id"] = int(row["defect_id"])
        by_id[row["defect_id"]].append(row)
    cleaned = []
    for variants in by_id.values():
        row = dict(variants[0])
        if len(variants) > 1:
            non_cost = [
                {key: value for key, value in v.items() if key != "repair_cost"} for v in variants
            ]
            costs = {str(v["repair_cost"]) for v in variants if v["repair_cost"] is not None}
            if any(v != non_cost[0] for v in non_cost[1:]) or len(costs) > 1:
                counters["conflicting_rows_quarantined"] += len(variants)
                continue
            row["repair_cost"] = next(
                (v["repair_cost"] for v in variants if v["repair_cost"] is not None), None
            )
            counters["compatible_or_normalized_copy_removed"] += len(variants) - 1
        cost = row["repair_cost"]
        row["repair_cost"] = (
            Decimal(str(cost)) if cost is not None and Decimal(str(cost)) >= 0 else None
        )
        cleaned.append(row)
    counters["clean_events"] = len(cleaned)
    return products, cleaned, dict(counters)


def independent_metrics(products, events, filters=None, scenario=""):
    filters = filters or {}
    start, end = filters.get("start_month", "2024-01"), filters.get("end_month", "2024-06")
    months = (int(end[:4]) - int(start[:4])) * 12 + int(end[5:]) - int(start[5:]) + 1
    cohort = {
        pid: p
        for pid, p in products.items()
        if all(
            not filters.get(key) or str(p[key]) == filters[key]
            for key in ("production_line", "category", "product_id")
        )
    }
    selected = []
    for e in events:
        if not start <= e["defect_date"][:7] <= end:
            continue
        product = products.get(e["product_id"])
        if any(
            filters.get(k) and (product is None or str(product[k]) != filters[k])
            for k in ("production_line", "category", "product_id")
        ):
            continue
        if any(
            filters.get(k) and e[k] != filters[k]
            for k in ("severity", "defect_location", "defect_type", "inspection_method")
        ):
            continue
        selected.append(e)
    linked = [e for e in selected if e["product_id"] in cohort]
    costs = [
        e["repair_cost"]
        for e in selected
        if e["repair_cost"] is not None
        and not (scenario == "exclude_cost_10" and e["repair_cost"] == 10)
    ]
    denominator = months * sum(p["monthly_output"] for p in cohort.values())
    return {
        "event_count": len(selected),
        "linked_event_count": len(linked),
        "critical_count": sum(e["severity"] == "Critical" for e in selected),
        "cost_known_count": len(costs),
        "cost_unknown_count": len(selected) - len(costs),
        "known_repair_cost": str(sum(costs, Decimal(0))),
        "cost_coverage_pct": 100 * len(costs) / len(selected) if selected else None,
        "cohort_product_count": len(cohort),
        "month_count": months,
        "reference_output": denominator if cohort else None,
        "reference_intensity_per_1000": 1000 * len(linked) / denominator if denominator else None,
        "method_counts": dict(Counter(e["inspection_method"] for e in selected)),
        "line_counts": dict(
            Counter(
                products.get(e["product_id"], {}).get("production_line", "Unknown")
                for e in selected
            )
        ),
        "defect_ids_sha256": sha(
            ",".join(
                str(e["defect_id"]) for e in sorted(selected, key=lambda x: x["defect_id"])
            ).encode()
        ),
    }


def freeze():
    if PROTOCOL.exists() or ORACLE.exists():
        raise SystemExit("Frozen protocol already exists; do not overwrite after outcomes")
    source_dir = STATE / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    with urlopen(
        Request(SOURCE_URL, headers={"User-Agent": "manufacturing-quality-independent-eval/1"}),
        timeout=45,
    ) as response:
        raw = response.read()
        source_meta = {
            "url": SOURCE_URL,
            "commit_context": SOURCE_COMMIT,
            "etag": response.headers.get("ETag"),
            "fetched_at_utc": now(),
            "content_type": response.headers.get("Content-Type"),
            "sha256": sha(raw),
            "schema": "array of defect records",
        }
    master = (ROOT / "data/master_data.csv").read_bytes()
    (source_dir / "defect_records.json").write_bytes(raw)
    (source_dir / "master_data.csv").write_bytes(master)
    products, events, reconciliation = independent_rows(raw, master)
    scopes = {
        "canonical": ({}, ""),
        "january": ({"start_month": "2024-01", "end_month": "2024-01"}, ""),
        "june": ({"start_month": "2024-06", "end_month": "2024-06"}, ""),
        "line3": ({"production_line": "Line-3"}, ""),
        "line3_critical": ({"production_line": "Line-3", "severity": "Critical"}, ""),
        "exclude_cost_10": ({}, "exclude_cost_10"),
        "empty_cohort": ({"production_line": "Line-1", "category": "Category 2"}, ""),
    }
    oracle = {
        "schema_version": 1,
        "created_at_utc": now(),
        "source": source_meta,
        "master_sha256": sha(master),
        "derivation": "stdlib only; independent_rows and independent_metrics in scripts/run_rebuild_eval.py",
        "reconciliation": reconciliation,
        "scopes": {
            key: {
                "filters": filters,
                "scenario": scenario,
                "expected": independent_metrics(products, events, filters, scenario),
            }
            for key, (filters, scenario) in scopes.items()
        },
    }
    protocol = {
        "schema_version": 1,
        "id": "rebuild-v1-20261016",
        "frozen_at_utc": now(),
        "scope": "Pinned manufacturing case acceptance; no general model-quality claim",
        "comparison": {
            "baseline": "Existing application before rebuild; live Not run because historical runtime budget exhausted",
            "intervention": "Rebuilt official Agents SDK runtime, composable backend tools and persistent investigation state",
            "known_baseline": {
                "python_tests": {
                    "status": "Passed",
                    "passed": 559,
                    "skipped": 1,
                    "elapsed_seconds": 103.82,
                    "evidence_origin": "coordinator-observed baseline",
                },
                "frontend_build": {
                    "status": "Passed",
                    "evidence_origin": "coordinator-observed baseline",
                },
                "live_tasks": {
                    "status": "Not run",
                    "reason": "old .runtime development budget exhausted; unrelated prior eval excluded",
                },
            },
        },
        "runtime": {
            "model": "gpt-6-luna",
            "reasoning_effort": "medium",
            "path": "FastAPI app HTTP -> Workspace -> official Agents SDK Runner -> managed Agents API -> actual backend tools",
            "history": "production local history only",
            "hosted_tools": False,
            "tracing": "local durable events only; hosted tracing disabled",
        },
        "source": source_meta,
        "master_sha256": sha(master),
        "oracle_sha256": sha((json.dumps(oracle, ensure_ascii=False, indent=2) + "\n").encode()),
        "protocol_freeze_working_tree_manifest": manifest(),
        "resource_limits": {
            "full_suite_usd": 5,
            "task_usd": 20,
            "per_turn_usd": 0.30,
            "max_output_tokens": 12000,
            "qa_max_retries": 2,
            "max_sdk_turns": 30,
            "task_timeout_seconds": 480,
            "cost_estimate_before_run_usd": {"low": 0.20, "high": 1.0},
        },
        "pricing": {
            "verified_at_utc": now(),
            "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
            "input_per_million": 0.1,
            "cached_input_per_million": 0.01,
            "cache_write_per_million": 0.125,
            "output_per_million": 0.5,
            "accounting": "cached is subset of input; reasoning is subset of output; missing details remain null",
        },
        "primary_metric": "Strict completed task count / 8 frozen cases; every required behavior, metric and artifact must have observed evidence",
        "secondary_metrics": [
            "numeric_correctness",
            "scope_correctness",
            "evidence_coverage",
            "unsupported_inference",
            "tool_errors",
            "latency",
            "cost",
            "cached_tokens/input_tokens",
        ],
        "scoring": "Deterministic oracle for arithmetic, scope, refs and actual artifact readback; manual observable rubric for prose and hypothesis quality; no model judge",
        "stopping_rule": "Fixed 8-case suite once stable; stop dispatch for provider stop, unknown unresolved usage, source/code drift or budget; preserve Failed/Blocked/Not run. Repairs use same development tasks and are labeled non-blind reruns.",
        "exclusions": [
            "Historical eval results and historical spend",
            "No hosted search, storage, sandbox, MCP or external sends",
            "Deterministic failure probes are separate simulated evidence and do not count as live task passes",
        ],
        "live_cases": cases(),
        "deterministic_categories": [
            "stale_scope_and_dataset",
            "two_session_isolation",
            "untrusted_data_and_authority",
            "cancel_reconnect_idempotency",
            "empty_scope_and_missing_denominator",
            "http_tool_model_failure",
            "schema_incomplete_refusal",
            "budget_during_artifact",
        ],
        "status_labels": sorted(STATUSES),
    }
    write_json(ORACLE, oracle)
    write_json(PROTOCOL, protocol)
    PROTOCOL.with_suffix(".sha256").write_text(sha(PROTOCOL.read_bytes()) + "\n", encoding="ascii")
    print(
        json.dumps(
            {
                "status": "Passed",
                "protocol_sha256": sha(PROTOCOL.read_bytes()),
                "cases": len(cases()),
                "planned_live_turns": sum(len(c["turns"]) for c in cases()),
                "oracle_scopes": len(scopes),
                "source_sha256": sha(raw),
                "master_sha256": sha(master),
            },
            indent=2,
        )
    )


def validate():
    protocol, oracle = read_json(PROTOCOL), read_json(ORACLE)
    if sha(PROTOCOL.read_bytes()) != PROTOCOL.with_suffix(".sha256").read_text().strip():
        raise ValueError("Frozen protocol bytes changed")
    if sha(ORACLE.read_bytes()) != protocol["oracle_sha256"]:
        raise ValueError("Frozen independent oracle bytes changed")
    if sha((ROOT / "data/master_data.csv").read_bytes()) != protocol["master_sha256"]:
        raise ValueError("Master source drift")
    return protocol, oracle


def ledger_rows():
    path = STATE / "task_spend.jsonl"
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def append_spend(row):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "task_spend.jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps({"time": now(), **row}, ensure_ascii=False, allow_nan=False) + "\n")


def task_cost():
    rows = ledger_rows()
    responses = {r["response_id"]: r for r in rows if r.get("kind") == "response_usage"}
    known = sum(
        r["cost_usd"]
        for r in responses.values()
        if r.get("cost_usd") is not None and r.get("cost_basis") == "provider_usage"
    )
    accounted = sum(
        r.get("cost_upper_bound_usd", r.get("cost_usd"))
        for r in responses.values()
        if r.get("cost_upper_bound_usd", r.get("cost_usd")) is not None
    )
    cache_unknown = [
        r["response_id"]
        for r in responses.values()
        if type(r.get("cached_tokens")) is not int or type(r.get("cache_write_tokens")) is not int
    ]
    unresolved = [
        r
        for r in rows
        if r.get("kind") == "turn_reservation"
        and not any(
            x.get("kind") == "turn_settlement" and x.get("run_id") == r["run_id"] for x in rows
        )
    ]
    return {
        "known_cost_usd": known,
        "exact_total_cost_usd": None if cache_unknown or unresolved else known,
        "conservative_accounted_usd": accounted,
        "response_count": len(responses),
        "unresolved_reserved_usd": sum(r["amount_usd"] for r in unresolved),
        "unknown_usage_responses": [
            r["response_id"] for r in responses.values() if not accountable_usage(r)
        ],
        "unavailable_cache_detail_responses": cache_unknown,
    }


def accountable_usage(row):
    bound = row.get("cost_upper_bound_usd", row.get("cost_usd"))
    return (
        row.get("provider_terminal") is True
        and type(row.get("input_tokens")) is int
        and type(row.get("output_tokens")) is int
        and min(row["input_tokens"], row["output_tokens"]) >= 0
        and isinstance(bound, (int, float))
        and not isinstance(bound, bool)
        and bound >= 0
    )


def independent_price(row, protocol):
    keys = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
    if any(type(row.get(key)) is not int for key in keys):
        return None
    inp, out, cached, written = (row[key] for key in keys)
    if min(inp, out, cached, written) < 0 or cached + written > inp:
        return None
    prices = protocol["pricing"]
    return (
        (inp - cached - written) * prices["input_per_million"]
        + cached * prices["cached_input_per_million"]
        + written * prices["cache_write_per_million"]
        + out * prices["output_per_million"]
    ) / 1_000_000


def retrieved_terminal_usage(response, protocol):
    """Price an existing terminal response without inventing missing telemetry."""
    if response.get("status") not in {"completed", "cancelled", "failed", "incomplete"}:
        return None
    if response.get("model") != "gpt-6-luna" or not isinstance(response.get("usage"), dict):
        return None
    usage = response["usage"]
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if type(inp) is not int or type(out) is not int or min(inp, out) < 0:
        return None
    detail = usage.get("input_tokens_details") or {}
    cached, written = detail.get("cached_tokens"), detail.get("cache_write_tokens")
    if any(
        value is not None and (type(value) is not int or value < 0) for value in (cached, written)
    ):
        return None
    known_cached = cached if cached is not None else 0
    bounded_writes = written if written is not None else inp - known_cached
    if known_cached + bounded_writes > inp or bounded_writes < 0:
        return None
    if any(
        item.get("type") in {"web_search_call", "file_search_call", "code_interpreter_call"}
        for item in response.get("output", [])
    ):
        return None
    prices = protocol["pricing"]
    bound = (
        (inp - known_cached - bounded_writes) * prices["input_per_million"]
        + known_cached * prices["cached_input_per_million"]
        + bounded_writes * prices["cache_write_per_million"]
        + out * prices["output_per_million"]
    ) / 1_000_000
    return {
        "response_id": response["id"],
        "model": response["model"],
        "requested_model": "gpt-6-luna",
        "provider_status": response["status"],
        "provider_terminal": True,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "cache_write_tokens": written,
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "hosted_tool_fees_usd": 0.0,
        "cost_basis": "provider_usage"
        if cached is not None and written is not None
        else "conservative_missing_cache_details",
        "cost_usd": bound,
        "cost_upper_bound_usd": bound,
        "elapsed_seconds": None,
        "reconciliation_origin": "bounded_read_only_retrieve_after_application_boundary",
        "provider_created_at": response.get("created_at"),
        "provider_completed_at": response.get("completed_at"),
        "service_tier": response.get("service_tier"),
    }


async def retrieve_existing_usage(api, missing_ids, output, protocol, allowance_seconds):
    """Reconcile historical Responses usage by known ID without generating or replaying work."""
    started = time.monotonic()
    deadline = started + max(0.0, min(30.0, allowance_seconds))
    records = {}
    attempts: list[dict[str, object]] = []
    provider_stop = False
    pending = set(missing_ids)
    while pending and time.monotonic() < deadline:
        for response_id in sorted(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = await asyncio.wait_for(
                    api.responses.retrieve(response_id), timeout=min(8.0, remaining)
                )
                raw = response.model_dump(mode="json")
                path = output / f"{response_id}-read{len(attempts) + 1}.json"
                write_json(path, raw)
                attempt = {
                    "requested_response_id": response_id,
                    "observed_response_id": raw.get("id"),
                    "status": raw.get("status"),
                    "raw_path": str(path),
                    "sha256": sha(path.read_bytes()),
                }
                attempts.append(attempt)
                if raw.get("id") != response_id:
                    continue
                failure_metadata = {
                    "error": raw.get("error"),
                    "incomplete_details": raw.get("incomplete_details"),
                }
                if "misalignment_policy_violation" in json.dumps(failure_metadata):
                    provider_stop = True
                record = retrieved_terminal_usage(raw, protocol)
                if record is not None:
                    records[response_id] = {
                        **record,
                        "raw_response_path": str(path),
                        "raw_response_sha256": attempt["sha256"],
                    }
                    pending.remove(response_id)
            except Exception as error:
                attempts.append(
                    {"requested_response_id": response_id, "error_type": type(error).__name__}
                )
        if pending and time.monotonic() < deadline:
            await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    return {
        "records": list(records.values()),
        "unresolved_response_ids": sorted(pending),
        "attempts": attempts,
        "provider_stop": provider_stop,
        "elapsed_seconds": time.monotonic() - started,
        "generation_requests_created": 0,
    }


def check(name, passed, evidence, observed=None, expected=None):
    return {
        "name": name,
        "status": "Passed" if passed else "Failed",
        "evidence": evidence,
        **(
            {"observed": observed, "expected": expected}
            if observed is not None or expected is not None
            else {}
        ),
    }


def same_number(actual, expected):
    if expected is None:
        return actual is None
    if isinstance(actual, bool) or actual is None:
        return False
    try:
        return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal("0.000001")
    except Exception:
        return False


def resolve_path(value, path):
    if not isinstance(path, str) or not path.startswith("$"):
        raise ValueError("Invalid numeric evidence path")
    position = 1
    while position < len(path):
        match = re.match(r"\.([^.[\]]+)|\[(\d+)\]", path[position:])
        if not match:
            raise ValueError("Unsupported numeric evidence path")
        value = value[match[1]] if match[1] is not None else value[int(match[2])]
        position += len(match[0])
    return value


def reference_checks(result, ledger):
    entries = {entry["execution_id"]: entry for entry in ledger}
    checks = []
    for claim in result.get("claims", []):
        receipt = entries.get(claim.get("execution_id"))
        try:
            receipt_result = receipt.get("result") if receipt else None
            valid = bool(
                receipt
                and claim.get("code_sha256") == receipt.get("code_sha256")
                and not (isinstance(receipt_result, dict) and receipt_result.get("ok") is False)
                and same_number(resolve_path(receipt["result"], claim["path"]), claim["value"])
            )
        except (ValueError, KeyError, IndexError, TypeError):
            valid = False
        checks.append(
            check(
                "claim_reference." + claim.get("id", "unknown"),
                valid,
                {"execution_id": claim.get("execution_id"), "path": claim.get("path")},
            )
        )
    previous = None
    for entry in ledger:
        encoded = json.dumps(
            entry["result"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode()
        checks.append(
            check(
                "receipt_seal." + entry["execution_id"],
                sha(encoded) == entry.get("result_sha256")
                and sha(entry.get("code", "").encode()) == entry.get("code_sha256"),
                "local append-only execution ledger",
            )
        )
        sealed = json.dumps(
            {key: value for key, value in entry.items() if key != "entry_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode()
        checks.append(
            check(
                "receipt_chain." + entry["execution_id"],
                sha(sealed) == entry.get("entry_sha256")
                and entry.get("previous_sha256") == previous,
                "independent append-only execution chain verification",
            )
        )
        previous = entry.get("entry_sha256")
    return checks


SCOPE_FIELDS = {
    "start_month",
    "end_month",
    "production_line",
    "category",
    "product_id",
    "inspection_method",
    "severity",
    "defect_type",
    "defect_location",
    "scenario",
}


def explicit_kpi_scope(payload):
    """Read declared output metadata, never select a scope by matching KPI values."""
    candidates = []
    if "scope" in payload:
        value = payload["scope"]
        if isinstance(value, dict) and "filters" in value:
            candidates.append((value["filters"], ".scope.filters"))
        else:
            candidates.append((value, ".scope"))
    if "context" in payload:
        context = payload["context"]
        candidates.append(
            (context.get("filters") if isinstance(context, dict) else None, ".context.filters")
        )
    if not candidates:
        return None, None, None
    parsed = []
    for value, path in candidates:
        if (
            not isinstance(value, dict)
            or set(value) != SCOPE_FIELDS
            or any(not isinstance(item, str) for item in value.values())
            or any(
                re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value[key]) is None
                for key in ("start_month", "end_month")
            )
            or value["start_month"] > value["end_month"]
        ):
            return None, path, "Explicit scope metadata is null, incomplete or invalid"
        parsed.append((value, path))
    if any(value != parsed[0][0] for value, _ in parsed[1:]):
        return None, None, "Conflicting explicit scope and context filters"
    return parsed[0][0], parsed[0][1], None


def kpi_checks(
    payload,
    products,
    events,
    evidence,
    inherited_scope=None,
    inherited_error=None,
    inherited_path=None,
    payload_path="$",
):
    if not isinstance(payload, dict):
        return []
    checks = []
    selected_scope, selected_path, scope_error = explicit_kpi_scope(payload)
    if inherited_error:
        scope_error = inherited_error
    elif (
        selected_scope is not None
        and inherited_scope is not None
        and selected_scope != inherited_scope
    ):
        scope_error = "Conflicting explicit scopes inside the same analysis context"
    if scope_error:
        selected_scope = None
    elif selected_scope is None:
        selected_scope = inherited_scope
        selected_path = inherited_path
    else:
        selected_path = payload_path + selected_path
    if isinstance(payload.get("kpis"), dict):
        if selected_scope is None or scope_error:
            checks.append(
                {
                    "name": "oracle.scope_binding",
                    "status": "Blocked",
                    "evidence": evidence,
                    "reason": scope_error or "No explicit scope metadata establishes this KPI set",
                    "payload_path": payload_path,
                }
            )
        else:
            wanted = independent_metrics(
                products, events, selected_scope, selected_scope["scenario"]
            )
            checks += [
                {
                    **check(
                        "oracle." + key,
                        same_number(payload["kpis"].get(key), value),
                        evidence,
                        payload["kpis"].get(key),
                        value,
                    ),
                    "scope": selected_scope,
                    "scope_evidence_path": selected_path,
                }
                for key, value in wanted.items()
                if key not in {"method_counts", "line_counts", "defect_ids_sha256"}
            ]
    for key in ("result", "payload", "analysis", "baseline", "comparison", "canonical", "selected"):
        if isinstance(payload.get(key), dict):
            comparison_branch = key in {"baseline", "comparison", "canonical", "selected"}
            checks += kpi_checks(
                payload[key],
                products,
                events,
                str(evidence) + ":" + key,
                None if comparison_branch else selected_scope,
                None if comparison_branch else scope_error,
                None if comparison_branch else selected_path,
                payload_path + "." + key,
            )
    return checks


def receipt_kpi_checks(entry, products, events):
    """Recognize only a declared whole-scope financial projection; keep full checks otherwise."""
    payload = entry.get("result")
    evidence = entry.get("execution_id", "unknown receipt")
    semantics = payload.get("scope_semantics") if isinstance(payload, dict) else None
    if not isinstance(semantics, dict) or not {
        "scope_total_path",
        "grouped_detail_path",
    }.intersection(semantics):
        return kpi_checks(payload, products, events, evidence)
    scope, scope_path, scope_error = explicit_kpi_scope(payload)
    context = payload.get("context")
    analysis = payload.get("analysis")
    financial_fields = (
        "event_count",
        "known_repair_cost",
        "cost_known_count",
        "cost_unknown_count",
        "cost_coverage_pct",
        "cost_available",
        "cost_status",
    )
    kpis = analysis.get("kpis") if isinstance(analysis, dict) else None
    costs = analysis.get("costs") if isinstance(analysis, dict) else None
    valid = (
        entry.get("tool") == "analysis_query"
        and entry.get("execution_kind") == "bounded_analysis"
        and payload.get("ok") is True
        and scope is not None
        and scope_error is None
        and isinstance(context, dict)
        and all(
            isinstance(context.get(key), str) and context[key].strip()
            for key in ("context_id", "snapshot_id", "dataset_version", "policy_version")
        )
        and context == entry.get("analysis_context")
        and context.get("scenario") == scope["scenario"]
        and semantics.get("kind") == "selected_scope"
        and semantics.get("filters_applied") is True
        and semantics.get("scenario_applied") is bool(scope["scenario"])
        and semantics.get("requested_context_id") == context.get("context_id")
        and semantics.get("requested_filters") == scope
        and semantics.get("scope_total_path") == "$.analysis.kpis"
        and semantics.get("grouped_detail_path") == "$.analysis.costs"
        and semantics.get("grain_note")
        == "kpis describe all logged defect events in the returned context; costs rows describe their named groups within that context."
        and isinstance(analysis, dict)
        and set(analysis) == {"kpis", "costs"}
        and isinstance(kpis, dict)
        and set(kpis) == set(financial_fields)
        and all(
            type(kpis[key]) is int
            for key in ("event_count", "cost_known_count", "cost_unknown_count")
        )
        and type(kpis["known_repair_cost"]) in {int, float}
        and (kpis["cost_coverage_pct"] is None or type(kpis["cost_coverage_pct"]) in {int, float})
        and type(kpis["cost_available"]) is bool
        and isinstance(kpis["cost_status"], str)
        and isinstance(costs, dict)
        and set(costs) == {"by_type", "by_location", "by_severity"}
        and all(
            isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
            for rows in costs.values()
        )
    )
    checks = [check("oracle.cost_projection_binding", valid, evidence)]
    if not valid:
        return checks + kpi_checks(payload, products, events, evidence)
    assert isinstance(kpis, dict)
    assert isinstance(analysis, dict)
    # Exclude only this verified leaf; all other KPI paths keep full traversal.
    remainder = {
        **payload,
        "analysis": {key: value for key, value in analysis.items() if key != "kpis"},
    }
    checks += kpi_checks(remainder, products, events, evidence)
    wanted = independent_metrics(products, events, scope, scope["scenario"])
    expected = {
        key: wanted[key] for key in financial_fields if key not in {"cost_available", "cost_status"}
    }
    known, total = wanted["cost_known_count"], wanted["event_count"]
    expected.update(
        cost_available=known > 0,
        cost_status="empty"
        if total == 0
        else "unavailable"
        if known == 0
        else "complete"
        if known == total
        else "partial",
    )
    for key, value in expected.items():
        exact = type(kpis[key]) is type(value) and kpis[key] == value
        checks.append(
            {
                **check(
                    "oracle." + key,
                    exact
                    if key in {"cost_available", "cost_status"}
                    else same_number(kpis[key], value),
                    evidence + ":analysis",
                    kpis[key],
                    value,
                ),
                "scope": scope,
                "scope_evidence_path": "$" + scope_path,
                "projection": "declared whole-scope financial KPIs",
            }
        )
    return checks


async def run_live(args):
    protocol, oracle = validate()
    if not args.live:
        raise SystemExit(
            "No API request dispatched. Use run --live for an authorized paid eval invocation."
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.suite_id):
        raise ValueError("suite-id must be a simple unique local identifier")
    output = STATE / "suites" / args.suite_id
    if output.exists():
        raise SystemExit(
            "Suite already exists. Use a fresh suite-id; never silently overwrite or replay paid work"
        )
    output.mkdir(parents=True)
    sys.path.insert(0, str(ROOT))
    import httpx
    from analyst.app import create_app
    from analyst.config import Settings, normalize_model_request
    from eval_browser_observer import EvalBrowserObserver

    frozen_manifest = manifest()
    frozen_evaluator_manifest = evaluator_manifest()
    for relative_path in frozen_evaluator_manifest:
        source_path = ROOT / relative_path
        (output / source_path.name).write_bytes(source_path.read_bytes())
    limits = protocol["resource_limits"]
    cost_before = task_cost()
    if cost_before["unresolved_reserved_usd"] or cost_before["unknown_usage_responses"]:
        raise SystemExit(
            "Prior generation usage is unresolved; reconcile it before another dispatch"
        )
    available = min(
        limits["full_suite_usd"],
        limits["task_usd"]
        - cost_before["conservative_accounted_usd"]
        - cost_before["unresolved_reserved_usd"],
    )
    settings = replace(
        Settings.from_env(ROOT),
        mode="live",
        model="gpt-6-luna",
        reasoning_effort="medium",
        # This historical protocol declares finite budgets and must opt into them.
        unbounded_runs=False,
        state_dir=str(output / "workspace"),
        dev_api_budget_usd=available,
        max_cost_usd_per_run=limits["per_turn_usd"],
        max_output_tokens=limits["max_output_tokens"],
        qa_max_retries=limits["qa_max_retries"],
        max_turns=limits["max_sdk_turns"],
        enable_file_search=False,
        enable_web_search=False,
        enable_code_interpreter=False,
        enable_mcp=False,
        enable_tool_search=False,
        multi_agent_native=False,
        memory_enabled=False,
        vector_store_id="",
        prompt_cache_key="rebuild-v1-luna-medium",
    )
    settings.validate()
    if available < limits["per_turn_usd"]:
        raise SystemExit("Task budget has no room for a conservatively reserved turn")
    app = create_app(settings)
    workspace = app.state.workspace
    original_emit = workspace.emit
    active_case: dict[str, object] = {"id": None, "turn": None}

    async def observed_emit(sid, rid, kind, data):
        emitted = await original_emit(sid, rid, kind, data)
        if kind == "usage" and data.get("response_id"):
            append_spend(
                {
                    "kind": "response_usage",
                    "suite_id": args.suite_id,
                    "case_id": active_case["id"],
                    "turn": active_case["turn"],
                    "session_id": sid,
                    "run_id": rid,
                    **data,
                    "independent_price_usd": independent_price(data, protocol),
                    "basis": "provider usage and frozen public token prices; no hosted tool fees",
                }
            )
            print(
                json.dumps(
                    {
                        "event": "usage",
                        "case": active_case["id"],
                        "turn": active_case["turn"],
                        "response_id": data["response_id"],
                        "cost_usd": data.get("cost_usd"),
                    }
                ),
                flush=True,
            )
        return emitted

    workspace.emit = observed_emit
    suite = {
        "schema_version": 1,
        "suite_id": args.suite_id,
        "evidence_mode": "live",
        "attempt_interpretation": "nonblind development rerun of unchanged frozen tasks"
        if cost_before["response_count"]
        else "first frozen development attempt; live baseline not run",
        "started_at_utc": now(),
        "protocol_sha256": sha(PROTOCOL.read_bytes()),
        "oracle_sha256": sha(ORACLE.read_bytes()),
        "harness_sha256": sha(Path(__file__).read_bytes()),
        "source_manifest": frozen_manifest,
        "evaluator_source_manifest": frozen_evaluator_manifest,
        "browser_observer": {
            "enabled": args.observe_ui,
            "case_ids": ["R02", "R03"] if args.observe_ui else [],
            "setup_wait_included_in_model_latency": False,
            "evidence_mode": "root_cua" if args.observe_ui else None,
        },
        "runtime": {
            "model": settings.model,
            "effort": settings.reasoning_effort,
            "openai_version": version("openai"),
            "agents_sdk_version": version("openai-agents"),
            "python_version": sys.version,
            "path": "actual FastAPI ASGI HTTP app -> production Workspace -> official SDK Runner -> managed Agents API",
            "hosted_trace_export": False,
            "max_sdk_turns": settings.max_turns,
            "qa_max_retries": settings.qa_max_retries,
            "max_output_tokens": settings.max_output_tokens,
            "application_run_timeout_seconds": settings.run_timeout_seconds,
            "harness_wait_timeout_seconds": limits["task_timeout_seconds"],
            "normalized_request_profile": normalize_model_request(settings),
        },
        "cost_before": cost_before,
        "cases": [],
        "stop_reason": None,
    }
    products, events, _ = independent_rows(
        (STATE / "source/defect_records.json").read_bytes(),
        (STATE / "source/master_data.csv").read_bytes(),
    )
    selected = [c for c in protocol["live_cases"] if not args.case or c["id"] in args.case]
    if set(args.case) - {c["id"] for c in selected}:
        raise ValueError("Unknown frozen case")
    suite_file = ROOT / "evals" / f"rebuild_results_{args.suite_id}.json"
    observer = None
    observer_cleanup_error = None
    try:
        if args.observe_ui:
            observer = EvalBrowserObserver(app, workspace, output / "browser-observer")
            await observer.start()
            suite["browser_observer"]["assets"] = observer.assets
            suite["browser_observer"]["url"] = observer.url
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=60
        ) as client:
            for case in selected:
                pause_marker = output / "PAUSE_AFTER_TURN.txt"
                if pause_marker.is_file():
                    suite["stop_reason"] = (
                        "Coordinator pause at a completed turn boundary: "
                        + pause_marker.read_text(encoding="utf-8")[:2000].strip()
                    )
                if suite["stop_reason"]:
                    suite["cases"].append(
                        {
                            "case_id": case["id"],
                            "status": "Not run",
                            "reason": suite["stop_reason"],
                            "turns": [],
                        }
                    )
                    continue
                if (
                    manifest() != frozen_manifest
                    or evaluator_manifest() != frozen_evaluator_manifest
                ):
                    suite["stop_reason"] = (
                        "Application or evaluator source drifted during eval; restart only after code is stable"
                    )
                    suite["cases"].append(
                        {
                            "case_id": case["id"],
                            "status": "Blocked",
                            "reason": suite["stop_reason"],
                            "turns": [],
                        }
                    )
                    continue
                response = await client.get("/api/dashboard", params=case["filters"])
                response.raise_for_status()
                context = response.json()
                if context["context"]["source_sha256"] != oracle["source"]["sha256"]:
                    raise RuntimeError(
                        "Fresh backend HTTP source differs from frozen independent source"
                    )
                sid = (await client.post("/api/sessions")).json()["id"]
                context_id = context["context"]["context_id"]
                case_out = {
                    "case_id": case["id"],
                    "name": case["name"],
                    "session_id": sid,
                    "initial_context": context["context"],
                    "turns": [],
                    "status": "Not run",
                    "requirements": [
                        {
                            "name": name,
                            "status": "Not run",
                            "reason": "Awaiting explicit evidence review",
                        }
                        for name in case["requirements"]
                    ],
                }
                suite["cases"].append(case_out)
                write_json(output / f"{case['id']}_initial_context.json", context)
                if observer and case["id"] in {"R02", "R03"}:
                    await workspace.set_ui_scope(sid, context_id)
                    gate = observer.publish(case["id"], sid, context, phase="predispatch")
                    write_json(suite_file, suite)
                    case_out["browser_observations"] = [await observer.wait(gate)]
                    write_json(suite_file, suite)
                for index, prompt in enumerate(case["turns"]):
                    active_case.update(id=case["id"], turn=index + 1)
                    if pause_marker.is_file():
                        suite["stop_reason"] = (
                            "Coordinator pause at a completed turn boundary: "
                            + pause_marker.read_text(encoding="utf-8")[:2000].strip()
                        )
                        break
                    if (
                        manifest() != frozen_manifest
                        or evaluator_manifest() != frozen_evaluator_manifest
                    ):
                        suite["stop_reason"] = "Application or evaluator source drifted during eval"
                        break
                    current_cost = task_cost()
                    suite_accounted = (
                        current_cost["conservative_accounted_usd"]
                        - cost_before["conservative_accounted_usd"]
                    )
                    if (
                        suite_accounted + limits["per_turn_usd"] > limits["full_suite_usd"]
                        or current_cost["conservative_accounted_usd"]
                        + current_cost["unresolved_reserved_usd"]
                        + limits["per_turn_usd"]
                        > limits["task_usd"]
                        or current_cost["unknown_usage_responses"]
                    ):
                        suite["stop_reason"] = (
                            "Budget/unknown-usage gate prevents another model dispatch"
                        )
                        break
                    language = case.get("turn_languages", [case["language"]] * len(case["turns"]))[
                        index
                    ]
                    started = time.monotonic()
                    print(
                        json.dumps(
                            {
                                "event": "turn_start",
                                "case": case["id"],
                                "turn": index + 1,
                                "language": language,
                            }
                        ),
                        flush=True,
                    )
                    response = await client.post(
                        f"/api/sessions/{sid}/prompt",
                        json={
                            "text": prompt,
                            "analysis_context_id": context_id,
                            "language": language,
                            "action_intent": "none",
                        },
                    )
                    response.raise_for_status()
                    await asyncio.sleep(0)
                    task = workspace.tasks[sid]
                    running = workspace.store.session(sid)["runs"][-1]
                    rid = running["id"]
                    append_spend(
                        {
                            "kind": "turn_reservation",
                            "suite_id": args.suite_id,
                            "case_id": case["id"],
                            "run_id": rid,
                            "amount_usd": limits["per_turn_usd"],
                        }
                    )
                    timed_out = False
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=limits["task_timeout_seconds"]
                        )
                    except TimeoutError:
                        timed_out = True
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    application_boundary_latency = time.monotonic() - started
                    session = (await client.get(f"/api/sessions/{sid}")).json()
                    row = workspace.store.db.execute(
                        "SELECT result,state FROM runs WHERE id=?", (rid,)
                    ).fetchone()
                    result = json.loads(row["result"]) if row["result"] else {}
                    run_dir = output / "workspace/sessions" / sid / "runs" / rid
                    ledger = (
                        [
                            json.loads(line)
                            for line in (run_dir / "ledger.jsonl")
                            .read_text(encoding="utf-8")
                            .splitlines()
                        ]
                        if (run_dir / "ledger.jsonl").exists()
                        else []
                    )
                    own_events = [e for e in session["events"] if e.get("run_id") == rid]
                    usage = {
                        e["data"]["response_id"]: e["data"]
                        for e in own_events
                        if e["type"] == "usage" and e["data"].get("response_id")
                    }
                    started_requests = sum(e["type"] == "model_started" for e in own_events)
                    created_ids = {
                        e["data"]["response_id"]
                        for e in own_events
                        if e["type"] == "response_created" and e["data"].get("response_id")
                    }
                    reconciliation = None
                    missing_ids = created_ids - usage.keys()
                    if missing_ids and len(created_ids) == started_requests:
                        from openai import AsyncOpenAI

                        async with AsyncOpenAI(
                            api_key=settings.api_key, max_retries=0, timeout=8
                        ) as accounting_api:
                            reconciliation = await retrieve_existing_usage(
                                accounting_api,
                                missing_ids,
                                output / "read_only_reconciliation" / rid,
                                protocol,
                                limits["task_timeout_seconds"] - (time.monotonic() - started),
                            )
                        for reconciled in reconciliation["records"]:
                            response_id = reconciled["response_id"]
                            assert response_id not in usage
                            usage[response_id] = reconciled
                            append_spend(
                                {
                                    "kind": "response_usage",
                                    "suite_id": args.suite_id,
                                    "case_id": case["id"],
                                    "turn": index + 1,
                                    "session_id": sid,
                                    "run_id": rid,
                                    **reconciled,
                                    "independent_price_usd": independent_price(
                                        reconciled, protocol
                                    ),
                                }
                            )
                            workspace.store.record_cost(rid, response_id, reconciled["cost_usd"])
                            await workspace.emit(sid, rid, "usage_reconciled", reconciled)
                        session = (await client.get(f"/api/sessions/{sid}")).json()
                        own_events = [e for e in session["events"] if e.get("run_id") == rid]
                    usage_complete = bool(usage) and all(
                        accountable_usage(r) for r in usage.values()
                    )
                    if usage_complete and started_requests == len(usage):
                        append_spend(
                            {
                                "kind": "turn_settlement",
                                "suite_id": args.suite_id,
                                "run_id": rid,
                                "accounted_cost_upper_bound_usd": sum(
                                    r.get("cost_upper_bound_usd", r["cost_usd"])
                                    for r in usage.values()
                                ),
                                "exact_cost_usd": sum(r["cost_usd"] for r in usage.values())
                                if all(
                                    r.get("cost_basis") == "provider_usage" for r in usage.values()
                                )
                                else None,
                            }
                        )
                        workspace.store.settle_budget(rid)
                    elif started_requests:
                        suite["stop_reason"] = (
                            "A dispatched model request has unresolved usage; conservative reservation remains"
                        )
                    if row["state"] == "provider_stopped":
                        suite["stop_reason"] = (
                            "Provider stopped affected conversation; do not retry the stopped session"
                        )
                    if reconciliation and reconciliation["provider_stop"]:
                        suite["stop_reason"] = (
                            "Provider stop observed during read-only reconciliation; no further dispatch"
                        )
                    readbacks = []
                    messages = [
                        e["data"]
                        for e in own_events
                        if e["type"] == "message" and e["data"].get("role") == "assistant"
                    ]
                    for artifact in (
                        messages[-1].get("delivery", {}).get("artifacts", []) if messages else []
                    ):
                        if not artifact.get("url"):
                            readbacks.append(
                                {
                                    "path": artifact.get("path"),
                                    "status": "Failed",
                                    "reason": "Delivery artifact has no local API URL",
                                }
                            )
                            continue
                        fetched = await client.get(artifact["url"])
                        saved = (
                            output
                            / "downloads"
                            / case["id"]
                            / str(index + 1)
                            / Path(artifact.get("path", "artifact.bin")).name
                        )
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        saved.write_bytes(fetched.content)
                        readbacks.append(
                            {
                                "path": artifact.get("path"),
                                "url": artifact["url"],
                                "http_status": fetched.status_code,
                                "sha256": sha(fetched.content),
                                "expected_sha256": artifact.get("sha256"),
                                "saved": str(saved.relative_to(ROOT)),
                                "bytes": len(fetched.content),
                                "status": "Passed"
                                if fetched.status_code == 200
                                and sha(fetched.content) == artifact.get("sha256")
                                else "Failed",
                            }
                        )
                    checks = [
                        check(
                            "actual_provider_model",
                            bool(usage)
                            and all(
                                u.get("model") == "gpt-6-luna"
                                and u.get("requested_model") == "gpt-6-luna"
                                for u in usage.values()
                            ),
                            "durable per-response usage",
                        ),
                        check(
                            "application_completed",
                            row["state"] == "completed"
                            and result.get("qa", {}).get("verified") is True,
                            "run state and application QA",
                            row["state"],
                            "completed",
                        ),
                        check(
                            "source_unchanged",
                            manifest() == frozen_manifest,
                            "pre/post application manifest",
                        ),
                        check(
                            "artifact_readback",
                            bool(readbacks) and all(a["status"] == "Passed" for a in readbacks),
                            "delivery artifacts fetched through owning session/run API",
                        ),
                    ]
                    checks += kpi_checks(
                        context.get("analysis", {}), products, events, "bound dashboard context"
                    )
                    for entry in ledger:
                        checks += receipt_kpi_checks(entry, products, events)
                    checks += reference_checks(result, ledger)
                    investigation_response = await client.get(f"/api/sessions/{sid}/investigation")
                    investigation = (
                        investigation_response.json()
                        if investigation_response.status_code == 200
                        else None
                    )
                    investigation_export = None
                    if investigation and investigation.get("version", 0) > 0:
                        exported_state = await client.get(
                            f"/api/sessions/{sid}/investigation/export.json",
                            params={"version": investigation["version"], "language": language},
                        )
                        export_path = (
                            output
                            / "downloads"
                            / case["id"]
                            / str(index + 1)
                            / f"current-investigation-{language}.json"
                        )
                        export_path.parent.mkdir(parents=True, exist_ok=True)
                        export_path.write_bytes(exported_state.content)
                        export_payload = (
                            exported_state.json() if exported_state.status_code == 200 else {}
                        )
                        export_ok = (
                            exported_state.status_code == 200
                            and export_payload.get("investigation") == investigation
                        )
                        investigation_export = {
                            "status": "Passed" if export_ok else "Failed",
                            "http_status": exported_state.status_code,
                            "version": investigation["version"],
                            "sha256": sha(exported_state.content),
                            "etag": exported_state.headers.get("etag"),
                            "saved": str(export_path.relative_to(ROOT)),
                            "state_matches_readback": export_payload.get("investigation")
                            == investigation,
                        }
                        checks.append(
                            check("current_investigation_export", export_ok, investigation_export)
                        )
                    turn_out = {
                        "turn": index + 1,
                        "prompt": prompt,
                        "language": language,
                        "run_id": rid,
                        "bound_context_id": context_id,
                        "raw_state": row["state"],
                        "timed_out": timed_out,
                        "application_boundary_latency_seconds": application_boundary_latency,
                        "latency_seconds": time.monotonic() - started,
                        "read_only_usage_reconciliation": reconciliation,
                        "usage": list(usage.values()),
                        "report": result.get("report"),
                        "claims": result.get("claims", []),
                        "qa": result.get("qa"),
                        "versions": {
                            key: result.get(key)
                            for key in (
                                "instructions_version",
                                "tools_version",
                                "report_number_format",
                            )
                        },
                        "model_dispatches": [
                            event["data"]
                            for event in own_events
                            if event["type"] == "model_started"
                        ],
                        "delivery": messages[-1].get("delivery") if messages else None,
                        "artifact_readbacks": readbacks,
                        "tools": [
                            {
                                "tool": e.get("tool"),
                                "execution_id": e.get("execution_id"),
                                "code_sha256": e.get("code_sha256"),
                                "result_sha256": e.get("result_sha256"),
                                "ok": e.get("result", {}).get("ok")
                                if isinstance(e.get("result"), dict)
                                else None,
                                "status": e.get("result", {}).get("status")
                                if isinstance(e.get("result"), dict)
                                else None,
                                "result_reference": e.get("result", {}).get("result_reference")
                                if isinstance(e.get("result"), dict)
                                else None,
                                "scope": e.get("result", {}).get(
                                    "scope", e.get("analysis_context", {}).get("filters")
                                )
                                if isinstance(e.get("result"), dict)
                                else e.get("analysis_context", {}).get("filters"),
                            }
                            for e in ledger
                        ],
                        "checks": checks,
                        "investigation": investigation,
                        "investigation_export": investigation_export,
                        "evidence_directory": str(run_dir.relative_to(ROOT)),
                        "evidence_sha256": {
                            p.name: sha(p.read_bytes()) for p in run_dir.glob("*.json*")
                        },
                    }
                    case_out["turns"].append(turn_out)
                    write_json(output / f"{case['id']}_turn{index + 1}_raw_result.json", result)
                    write_json(output / f"{case['id']}_turn{index + 1}_session.json", session)
                    write_json(suite_file, suite)
                    print(
                        json.dumps(
                            {
                                "event": "turn_done",
                                "case": case["id"],
                                "turn": index + 1,
                                "state": row["state"],
                                "latency_seconds": turn_out["latency_seconds"],
                                "application_boundary_latency_seconds": application_boundary_latency,
                                "reconciliation_elapsed_seconds": reconciliation["elapsed_seconds"]
                                if reconciliation
                                else 0,
                                "responses": len(usage),
                                "failed_checks": sum(c["status"] == "Failed" for c in checks),
                            }
                        ),
                        flush=True,
                    )
                    ui_events = [e["data"] for e in own_events if e["type"] == "ui_command"]
                    if ui_events:
                        context_id = (
                            ui_events[-1].get("context_id")
                            or ui_events[-1].get("target_context_id")
                            or context_id
                        )
                        if investigation and investigation.get("ui", {}).get("context_id"):
                            context_id = investigation["ui"]["context_id"]
                    if (
                        observer
                        and case["id"] in {"R02", "R03"}
                        and (
                            not started_requests
                            or usage_complete
                            and started_requests == len(usage)
                        )
                    ):
                        observed_ui = workspace.store.investigation(sid)["ui"]
                        target = await workspace.analysis.context_by_id(observed_ui["context_id"])
                        gate = observer.publish(
                            case["id"],
                            sid,
                            target,
                            phase=f"turn_{index + 1}_complete",
                            command_id=observed_ui.get("last_command_id"),
                        )
                        observation = await observer.wait(gate)
                        case_out["browser_observations"].append(observation)
                        turn_out["browser_observation"] = observation
                        write_json(suite_file, suite)
                    elif observer and case["id"] in {"R02", "R03"}:
                        turn_out["browser_observation"] = {
                            "status": "Not run",
                            "reason": "Generation usage is unresolved; no observation wait or further dispatch",
                        }
                    if row["state"] not in {"completed", "unverified"}:
                        break
                case_out["status"] = (
                    "Failed"
                    if any(
                        c["status"] == "Failed"
                        for turn in case_out["turns"]
                        for c in turn["checks"]
                    )
                    else "Not run"
                )
                if len(case_out["turns"]) != len(case["turns"]) and not case_out["turns"]:
                    case_out["status"] = "Blocked"
                write_json(suite_file, suite)
    finally:
        if observer:
            try:
                await observer.stop()
            except Exception as exc:
                observer_cleanup_error = repr(exc)
                suite["browser_observer"]["shutdown_error"] = observer_cleanup_error
        await workspace.close()
        suite["finished_at_utc"] = now()
        suite["cost_after"] = task_cost()
        suite["source_manifest_after"] = manifest()
        suite["evaluator_source_manifest_after"] = evaluator_manifest()
        planned = {case["id"]: case for case in selected}
        for case_result in suite["cases"]:
            observed = len(case_result["turns"])
            planned_turns = len(planned[case_result["case_id"]]["turns"])
            case_result["planned_turn_count"] = planned_turns
            case_result["observed_turn_count"] = observed
            case_result["unrun_turns"] = [
                {
                    "turn": index + 1,
                    "status": "Not run",
                    "reason": suite["stop_reason"] or "Prerequisite turn did not complete",
                }
                for index in range(observed, planned_turns)
            ]
        suite["known_suite_cost_usd"] = (
            suite["cost_after"]["known_cost_usd"] - cost_before["known_cost_usd"]
        )
        suite["accounted_suite_cost_upper_bound_usd"] = (
            suite["cost_after"]["conservative_accounted_usd"]
            - cost_before["conservative_accounted_usd"]
        )
        suite["exact_suite_cost_usd"] = (
            suite["known_suite_cost_usd"]
            if not any(
                row.get("suite_id") == args.suite_id
                and row.get("kind") == "response_usage"
                and row.get("cost_basis") != "provider_usage"
                for row in ledger_rows()
            )
            and not suite["cost_after"]["unresolved_reserved_usd"]
            else None
        )
        suite["status"] = (
            "Failed" if any(c["status"] == "Failed" for c in suite["cases"]) else "Not run"
        )
        write_json(suite_file, suite)
        print(
            json.dumps(
                {
                    "suite_id": args.suite_id,
                    "status": suite["status"],
                    "known_suite_cost_usd": suite["known_suite_cost_usd"],
                    "exact_suite_cost_usd": suite["exact_suite_cost_usd"],
                    "accounted_suite_cost_upper_bound_usd": suite[
                        "accounted_suite_cost_upper_bound_usd"
                    ],
                    "cost_after": suite["cost_after"],
                    "stop_reason": suite["stop_reason"],
                    "results": str(suite_file),
                },
                indent=2,
            ),
            flush=True,
        )
        if observer_cleanup_error:
            raise RuntimeError("Observer cleanup failed: " + observer_cleanup_error)


async def preflight():
    """No model dispatch. Check source/oracle and the actual backend scope path."""
    protocol, oracle = validate()
    sys.path.insert(0, str(ROOT))
    import httpx
    from analyst.app import create_app
    from analyst.config import Settings

    settings = replace(
        Settings.from_env(ROOT),
        state_dir=str(STATE / "preflight"),
        memory_enabled=False,
        enable_file_search=False,
        vector_store_id="",
    )
    app = create_app(settings)
    results = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=60
        ) as client:
            for name, scope in oracle["scopes"].items():
                response = await client.get(
                    "/api/dashboard", params={**scope["filters"], "scenario": scope["scenario"]}
                )
                if response.status_code != 200:
                    results.append(
                        {
                            "name": name,
                            "status": "Failed",
                            "http_status": response.status_code,
                            "body": response.text,
                        }
                    )
                    continue
                payload = response.json()
                expected = scope["expected"]
                checks = [
                    check(
                        key,
                        same_number(payload["analysis"]["kpis"].get(key), value),
                        "source-independent stdlib oracle",
                        payload["analysis"]["kpis"].get(key),
                        value,
                    )
                    for key, value in expected.items()
                    if key not in {"method_counts", "line_counts", "defect_ids_sha256"}
                ]
                results.append(
                    {
                        "name": name,
                        "status": "Passed"
                        if all(c["status"] == "Passed" for c in checks)
                        else "Failed",
                        "context": payload["context"],
                        "checks": checks,
                    }
                )
    finally:
        await app.state.workspace.close()
    output = {
        "observed_at_utc": now(),
        "evidence_mode": "deterministic_current_backend_http_source",
        "model_dispatches": 0,
        "source_manifest": manifest(),
        "results": results,
        "status": "Passed" if all(r["status"] == "Passed" for r in results) else "Failed",
    }
    write_json(ROOT / "evals/rebuild_preflight.json", output)
    print(
        json.dumps(
            {
                "status": output["status"],
                "scopes": [{"name": r["name"], "status": r["status"]} for r in results],
                "model_dispatches": 0,
            },
            indent=2,
        )
    )


def independent_details():
    validate()
    products, events, _ = independent_rows(
        (STATE / "source/defect_records.json").read_bytes(),
        (STATE / "source/master_data.csv").read_bytes(),
    )
    selected = [e for e in events if "2024-01" <= e["defect_date"][:7] <= "2024-06"]
    lines = sorted({p["production_line"] for p in products.values()})
    line_costs = {
        line: {
            scenario: independent_metrics(products, events, {"production_line": line}, scenario)
            for scenario in ("", "exclude_cost_10")
        }
        for line in lines
    }
    decomposition = {}
    for dimension in ("production_line", "category", "product_id"):
        counts: Counter[tuple[str, str]] = Counter()
        for event in selected:
            label = (
                str(event["product_id"])
                if dimension == "product_id"
                else products.get(event["product_id"], {}).get(dimension, "Unknown")
            )
            counts[label, event["defect_date"][:7]] += 1
        rows: list[dict[str, str | int]] = [
            {
                "label": label,
                "january": counts[label, "2024-01"],
                "june": counts[label, "2024-06"],
                "delta": counts[label, "2024-06"] - counts[label, "2024-01"],
            }
            for label in {pair[0] for pair in counts}
        ]
        decomposition[dimension] = sorted(
            rows, key=lambda row: (-int(row["delta"]), str(row["label"]))
        )
    location_severity = Counter((e["defect_location"], e["severity"]) for e in selected)
    output = {
        "created_at_utc": now(),
        "derivation": "Supplementary stdlib raw-source arithmetic for frozen requirements; no model dispatch or production analytics imports",
        "line_costs": line_costs,
        "june_minus_january": decomposition,
        "location_severity": [
            {"location": key[0], "severity": key[1], "events": count}
            for key, count in sorted(location_severity.items())
        ],
    }
    write_json(ROOT / "evals/rebuild_independent_detail.json", output)
    print(
        json.dumps(
            {
                "status": "Passed",
                "delta_by_dimension": {
                    key: sum(int(row["delta"]) for row in rows)
                    for key, rows in decomposition.items()
                },
                "method_counts": independent_metrics(products, events)["method_counts"],
            },
            indent=2,
        )
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["freeze", "validate", "preflight", "details", "run", "summarize"]
    )
    parser.add_argument("--suite-id", default="post-v1")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument(
        "--observe-ui",
        action="store_true",
        help="Pause R02/R03 before dispatch and after terminal turns for root CUA observation; waits are outside model latency",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly authorize this CLI invocation to dispatch the already-owner-authorized Luna eval",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.command == "freeze":
        freeze()
    elif args.command == "validate":
        protocol, oracle = validate()
        print(
            json.dumps(
                {
                    "status": "Passed",
                    "protocol_sha256": sha(PROTOCOL.read_bytes()),
                    "cases": len(protocol["live_cases"]),
                    "oracle_canonical": oracle["scopes"]["canonical"]["expected"],
                },
                indent=2,
            )
        )
    elif args.command == "run":
        asyncio.run(run_live(args))
    elif args.command == "preflight":
        asyncio.run(preflight())
    elif args.command == "details":
        independent_details()
    else:
        print(json.dumps(task_cost(), indent=2))
