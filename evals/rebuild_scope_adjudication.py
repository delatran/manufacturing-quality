"""Replay frozen KPI checks with explicit scope metadata. Never calls a model."""

from __future__ import annotations

import argparse
import ast
import copy
import inspect
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_rebuild_eval import (  # noqa: E402
    SCOPE_FIELDS,
    explicit_kpi_scope,
    independent_rows,
    kpi_checks,
    now,
    reference_checks,
    sha,
    validate,
    write_json,
)


def source_function(path, name):
    source = path.read_text(encoding="utf-8")
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.get_source_segment(source, node)


def scope_counterexamples(products, events, oracle, full_scope):
    checks = []

    def payload(scope_name, metric_name=None):
        item = oracle["scopes"][scope_name]
        filters = {**full_scope, **item["filters"], "scenario": item["scenario"]}
        expected = oracle["scopes"][metric_name or scope_name]["expected"]
        return {"context": {"filters": filters}, "analysis": {"kpis": copy.deepcopy(expected)}}

    def run(name, value, condition):
        observed = kpi_checks(value, products, events, name)
        assert condition(observed), (name, observed)
        checks.append({"name": name, "status": "Passed", "observed_checks": observed})

    def all_passed(rows):
        return bool(rows) and all(row["status"] == "Passed" for row in rows)

    def blocked(rows):
        return any(row["status"] == "Blocked" for row in rows)

    run("explicit_parent_line3_context_is_inherited", payload("line3"), all_passed)
    run(
        "canonical_label_cannot_accept_line3_values",
        payload("canonical", "line3"),
        lambda rows: any(
            row["name"] == "oracle.event_count"
            and row["status"] == "Failed"
            and row["observed"] == 282
            and row["expected"] == 1071
            for row in rows
        ),
    )
    wrong_line = payload("line3")
    wrong_line["context"]["filters"]["production_line"] = "Line-4"
    run(
        "wrong_line_label_rejects_line3_values",
        wrong_line,
        lambda rows: any(row["status"] == "Failed" for row in rows),
    )
    comparison = {"baseline": payload("january"), "comparison": payload("june")}
    run("nested_periods_keep_their_own_explicit_contexts", comparison, all_passed)
    swapped = copy.deepcopy(comparison)
    swapped["comparison"]["analysis"]["kpis"] = copy.deepcopy(
        oracle["scopes"]["january"]["expected"]
    )
    run(
        "mixed_nested_comparison_cannot_infer_scope_from_matching_values",
        swapped,
        lambda rows: any(
            row["name"] == "oracle.event_count"
            and row["status"] == "Failed"
            and row["observed"] == 147
            and row["expected"] == 252
            for row in rows
        ),
    )
    null_scope = payload("line3")
    null_scope["analysis"]["scope"] = None
    run("explicit_null_scope_blocks_parent_fallback", null_scope, blocked)
    run(
        "missing_scope_is_not_assumed_canonical",
        {"kpis": copy.deepcopy(oracle["scopes"]["canonical"]["expected"])},
        blocked,
    )
    partial_scope = payload("line3")
    partial_scope["analysis"]["scope"] = {"production_line": "Line-3"}
    run("partial_output_scope_is_ambiguous", partial_scope, blocked)
    conflict = payload("canonical", "line3")
    conflict["analysis"]["scope"] = {**full_scope, "production_line": "Line-3"}
    run("analysis_scope_cannot_conflict_with_its_wrapper_context", conflict, blocked)
    same_level = payload("line3")
    same_level["scope"] = full_scope
    run("same_level_scope_context_conflict_is_blocked", same_level, blocked)
    branch_missing = payload("canonical")
    branch_missing["baseline"] = {
        "analysis": {"kpis": copy.deepcopy(oracle["scopes"]["canonical"]["expected"])}
    }
    run("comparison_branch_cannot_borrow_parent_context_without_metadata", branch_missing, blocked)
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite-id", default="post-v3")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    protocol, oracle = validate()
    original_path = ROOT / "evals" / f"rebuild_results_{args.suite_id}.json"
    original_bytes = original_path.read_bytes()
    original = json.loads(original_bytes)
    suite_dir = ROOT / ".runtime/rebuild-20261016/suites" / args.suite_id
    executed_harness = suite_dir / "run_rebuild_eval.executed.py"
    current_harness = ROOT / "scripts/run_rebuild_eval.py"
    unchanged_functions = {}
    for name in (
        "cases",
        "independent_rows",
        "independent_metrics",
        "same_number",
        "reference_checks",
    ):
        old = source_function(executed_harness, name)
        new = source_function(current_harness, name)
        assert old == new, f"Frozen arithmetic or task behavior changed: {name}"
        unchanged_functions[name] = sha(old.encode())
    assert original["source_manifest"] == original["source_manifest_after"]
    assert original["protocol_sha256"] == sha(
        (ROOT / "evals/rebuild_protocol_v1.json").read_bytes()
    )
    assert original["oracle_sha256"] == sha((ROOT / "evals/rebuild_oracle_v1.json").read_bytes())
    source_dir = ROOT / ".runtime/rebuild-20261016/source"
    assert sha((source_dir / "defect_records.json").read_bytes()) == oracle["source"]["sha256"]
    products, events, _ = independent_rows(
        (source_dir / "defect_records.json").read_bytes(),
        (source_dir / "master_data.csv").read_bytes(),
    )
    initial = json.loads((suite_dir / "R01_initial_context.json").read_text(encoding="utf-8"))
    full_scope = initial["context"]["filters"]
    assert set(full_scope) == SCOPE_FIELDS
    probes = scope_counterexamples(products, events, oracle, full_scope)
    manual_path = (
        ROOT
        / ".runtime/rebuild-20261016/independent-review"
        / f"{args.suite_id}-manual-review-v1.json"
    )
    manual_bytes = manual_path.read_bytes()
    manual = json.loads(manual_bytes)
    assert manual["reviewed_results_pre_adjudication_sha256"] == sha(original_bytes)
    assert manual["protocol_sha256"] == original["protocol_sha256"]
    assert manual["model_dispatches_by_reviewer"] == 0
    amended = copy.deepcopy(original)
    changes = []
    failed_before = Counter()
    failed_after = Counter()
    new_checks_count = 0
    for case in amended["cases"]:
        frozen_case = next(item for item in protocol["live_cases"] if item["id"] == case["case_id"])
        review = next(item for item in manual["cases"] if item["id"] == case["case_id"])
        assert set(review["requirements"]) == set(frozen_case["requirements"])
        context = json.loads(
            (suite_dir / f"{case['case_id']}_initial_context.json").read_text(encoding="utf-8")
        )
        assert context["context"]["source_sha256"] == oracle["source"]["sha256"]
        for turn in case["turns"]:
            ledger = [
                json.loads(line)
                for line in (ROOT / turn["evidence_directory"] / "ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            result = json.loads(
                (suite_dir / f"{case['case_id']}_turn{turn['turn']}_raw_result.json").read_text(
                    encoding="utf-8"
                )
            )
            seals = reference_checks(result, ledger)
            assert all(item["status"] == "Passed" for item in seals), (
                "Untrusted receipt or claim seal"
            )
            contextual = kpi_checks(
                context["analysis"], products, events, "bound dashboard context"
            )
            for entry in ledger:
                contextual += kpi_checks(
                    entry.get("result"),
                    products,
                    events,
                    entry.get("execution_id", "unknown receipt"),
                )
            old_oracle = [item for item in turn["checks"] if item["name"].startswith("oracle.")]
            assert len(contextual) == len(old_oracle), (
                "Scope correction must retain every original KPI check"
            )
            keyed = {(item["name"], str(item["evidence"])): item for item in contextual}
            assert len(keyed) == len(contextual)
            for old in old_oracle:
                new = keyed[(old["name"], str(old["evidence"]))]
                if old["status"] != new["status"] or old.get("expected") != new.get("expected"):
                    changes.append(
                        {
                            "case_id": case["case_id"],
                            "turn": turn["turn"],
                            "original": old,
                            "adjudicated": new,
                        }
                    )
            turn["adjudicated_checks"] = [
                item for item in turn["checks"] if not item["name"].startswith("oracle.")
            ] + contextual
            new_checks_count += len(contextual)
            failed_before.update(
                item["name"] for item in turn["checks"] if item["status"] == "Failed"
            )
            failed_after.update(
                item["name"] for item in turn["adjudicated_checks"] if item["status"] == "Failed"
            )
        case["initial_evaluator_status"] = case["status"]
        case["requirements"] = [
            {"name": name, **review["requirements"][name]} for name in frozen_case["requirements"]
        ]
        case["independent_review"] = {
            "status": review["status"],
            "notes": review["notes"],
            "reviewed_run_ids": review["reviewed_run_ids"],
        }
        case["status"] = review["status"]
        if case["status"] == "Passed":
            assert len(case["turns"]) == len(frozen_case["turns"])
            assert all(item["status"] == "Passed" for item in case["requirements"])
            assert all(
                item["status"] == "Passed"
                for turn in case["turns"]
                for item in turn["adjudicated_checks"]
            )
    receipt = {
        "observed_at_utc": now(),
        "evidence_mode": "offline_replay_of_sealed_live_receipts_plus_independent_human_review",
        "generation_requests_created": 0,
        "network_requests": 0,
        "reason": "The original checker ignored result.context.filters and assumed canonical scope for nested KPI sets. The correction uses explicit nearest metadata and rejects ambiguous/conflicting scopes. Expected values and rubric are unchanged.",
        "original_results_path": str(original_path.relative_to(ROOT)),
        "original_results_sha256": sha(original_bytes),
        "executed_harness_sha256": sha(executed_harness.read_bytes()),
        "corrected_harness_sha256": sha(current_harness.read_bytes()),
        "corrected_scope_function_sha256": sha(
            (inspect.getsource(explicit_kpi_scope) + inspect.getsource(kpi_checks)).encode()
        ),
        "scope_fields": sorted(SCOPE_FIELDS),
        "unchanged_function_source_hashes": unchanged_functions,
        "protocol_sha256": original["protocol_sha256"],
        "oracle_sha256": original["oracle_sha256"],
        "manual_review_path": str(manual_path.relative_to(ROOT)),
        "manual_review_sha256": sha(manual_bytes),
        "counterexample_checks": probes,
        "kpi_checks_replayed": new_checks_count,
        "changed_check_count": len(changes),
        "original_failed_check_counts": dict(failed_before),
        "adjudicated_failed_check_counts": dict(failed_after),
        "changes": changes,
    }
    amended["scope_adjudication"] = {
        key: value
        for key, value in receipt.items()
        if key not in ("counterexample_checks", "changes")
    }
    amended["strict_task_completion"] = {
        "passed": sum(case["status"] == "Passed" for case in amended["cases"]),
        "failed": sum(case["status"] == "Failed" for case in amended["cases"]),
        "blocked": sum(case["status"] == "Blocked" for case in amended["cases"]),
        "not_run": sum(case["status"] == "Not run" for case in amended["cases"]),
        "denominator": 8,
        "rate": sum(case["status"] == "Passed" for case in amended["cases"]) / 8,
        "observed_turns": sum(len(case["turns"]) for case in amended["cases"]),
        "planned_turns": 12,
    }
    assert sha(original_path.read_bytes()) == sha(original_bytes)
    if not args.verify_only:
        target = ROOT / "evals" / f"rebuild_results_{args.suite_id}_adjudicated.json"
        receipt_path = ROOT / "evals" / f"rebuild_scope_adjudication_{args.suite_id}.json"
        if target.exists() or receipt_path.exists():
            raise SystemExit(
                "Adjudication already exists. Use --verify-only; never overwrite the original record."
            )
        write_json(target, amended)
        write_json(receipt_path, receipt)
    print(
        json.dumps(
            {
                "status": "Passed",
                "verify_only": args.verify_only,
                "counterexamples": len(probes),
                "kpi_checks_replayed": new_checks_count,
                "changed_scope_checks": len(changes),
                "real_failed_checks_retained": sum(failed_after.values()),
                "strict_task_completion": amended["strict_task_completion"],
                "original_unchanged": sha(original_path.read_bytes()) == sha(original_bytes),
            }
        )
    )


if __name__ == "__main__":
    main()
