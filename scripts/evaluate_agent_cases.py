"""Validate a frozen protocol and summarize reviewed artifacts without API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "evals" / "quality_operations_cases.json"
FROZEN_PROTOCOL_SHA256 = "14778ae30bb4f138247f2db9c0208314e4a369b19cec4427f69c72d307a6b40d"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_protocol(path: Path = PROTOCOL) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate(protocol: dict, path: Path = PROTOCOL) -> dict:
    errors = []
    if path.resolve() == PROTOCOL.resolve() and digest(path) != FROZEN_PROTOCOL_SHA256:
        errors.append("Frozen protocol bytes changed; record a new version before reuse")
    cases = protocol["cases"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("Case IDs are not unique")
    if protocol["baseline"]["model"] != "gpt-6-luna":
        errors.append("The protocol must use the authorized application model")
    for name in ("initial_paired_case_order", "additional_case_order"):
        if any(case_id not in ids for case_id in protocol["execution"][name]):
            errors.append(f"Unknown case in {name}")
    order = (
        protocol["execution"]["initial_paired_case_order"]
        + protocol["execution"]["additional_case_order"]
    )
    if len(order) != len(set(order)) or set(order) != set(ids):
        errors.append("Execution order must include each case exactly once")
    fixture_hashes = {}
    for key, fixture in protocol["fixtures"].items():
        target = path.parent / "fixtures" / fixture["filename"]
        expected = fixture["content"].encode("utf-8")
        if not target.is_file() or target.read_bytes() != expected:
            errors.append(f"Fixture bytes differ: {key}")
        fixture_hashes[key] = hashlib.sha256(expected).hexdigest()
    for case in cases:
        entry = case["input"]
        if entry["mode"] not in {"dashboard", "standalone"}:
            errors.append(f"Invalid source mode: {case['id']}")
        if entry["language"] not in {"en", "vi"}:
            errors.append(f"Invalid language: {case['id']}")
        if not entry["turns"] or any(not turn.get("prompt", "").strip() for turn in entry["turns"]):
            errors.append(f"Empty task wording: {case['id']}")
        if any(key not in protocol["fixtures"] for key in entry.get("fixtures", [])):
            errors.append(f"Unknown fixture: {case['id']}")
        evaluation = case["evaluation"]
        fields = [item["field"] for item in evaluation["numeric_assertions"]]
        if len(fields) != len(set(fields)):
            errors.append(f"Duplicate numeric field: {case['id']}")
        if not evaluation["required_behaviors"]:
            errors.append(f"No outcome criteria: {case['id']}")
    oracle = json.loads((ROOT / "knowledge" / "independent_oracle.json").read_text())
    for name in ("source_sha256", "master_sha256"):
        if oracle["provenance"][name] != protocol["baseline"][name]:
            errors.append(f"Oracle identity differs: {name}")
    if digest(ROOT / "data" / "master_data.csv") != protocol["baseline"]["master_sha256"]:
        errors.append("Current master bytes differ from the frozen protocol")
    return {
        "valid": not errors,
        "protocol_sha256": digest(path),
        "case_count": len(cases),
        "fixture_sha256": fixture_hashes,
        "errors": errors,
        "source_note": "This command does not fetch the remote source or call a model.",
    }


def public_input(protocol: dict, case_id: str) -> dict:
    case = next(item for item in protocol["cases"] if item["id"] == case_id)
    source = case["input"]
    return {
        "case_id": case_id,
        **{key: source[key] for key in ("language", "mode", "filters", "turns")},
        "attachments": [
            {
                "filename": protocol["fixtures"][key]["filename"],
                "path": str(ROOT / "evals" / "fixtures" / protocol["fixtures"][key]["filename"]),
            }
            for key in source.get("fixtures", [])
        ],
        "executor_setup_required": bool(source.get("setup")),
    }


def numeric_matches(observed: object, expected: dict) -> bool:
    if expected["value"] is None:
        return observed is None
    if isinstance(observed, bool) or observed is None:
        return False
    try:
        actual = Decimal(str(observed))
        target = Decimal(str(expected["value"]))
        tolerance = Decimal(str(expected.get("abs_tolerance", 0)))
        return actual.is_finite() and abs(actual - target) <= tolerance
    except (InvalidOperation, ValueError, TypeError):
        return False


def checked_pass(check: dict | None) -> bool:
    return bool(check and check.get("status") == "pass" and check.get("evidence_refs"))


def score_run(protocol: dict, run: dict) -> dict:
    case = next(item for item in protocol["cases"] if item["id"] == run["case_id"])
    required = case["evaluation"]
    reasons = []
    if run.get("evidence_mode") == "live":
        allowed_model = {protocol["baseline"]["model"]}
        if set(run.get("models_observed", [])) != allowed_model:
            reasons.append(
                "Actual provider model evidence is absent or violates the Luna-only condition"
            )
        if set(run.get("models_requested", [])) != allowed_model:
            reasons.append("Requested model evidence is absent or violates the Luna-only condition")
    identity = run.get("source_identity", {})
    fixture_ids = case["input"].get("fixtures", [])
    if fixture_ids:
        expected_hashes = {
            key: hashlib.sha256(protocol["fixtures"][key]["content"].encode("utf-8")).hexdigest()
            for key in fixture_ids
        }
        if identity.get("fixture_sha256") != expected_hashes:
            reasons.append("Observed uploaded-source identities differ or are unavailable")
    else:
        for key in ("source_sha256", "master_sha256"):
            if identity.get(key) != protocol["baseline"][key]:
                reasons.append(
                    f"Observed manufacturing source identity differs or is unavailable: {key}"
                )
    dimensions = run.get("dimensions", {})
    for name in protocol["rubric"]["dimensions"]:
        status = dimensions.get(name, "not_observed")
        allowed = {"pass", "not_applicable"} if name == "numeric_correctness" else {"pass"}
        if status not in allowed:
            reasons.append(f"Dimension {name}: {status}")
    behaviors = {item["index"]: item for item in run.get("behavior_checks", [])}
    if len(behaviors) != len(run.get("behavior_checks", [])):
        raise ValueError(f"Duplicate behavior index in {run['run_id']}")
    for index, description in enumerate(required["required_behaviors"]):
        if not checked_pass(behaviors.get(index)):
            reasons.append(f"Behavior {index} not evidenced: {description}")
    if set(behaviors) - set(range(len(required["required_behaviors"]))):
        raise ValueError(f"Unknown behavior index in {run['run_id']}")
    expected = {item["field"]: item for item in required["numeric_assertions"]}
    numbers = {item["field"]: item for item in run.get("numeric_checks", [])}
    if len(numbers) != len(run.get("numeric_checks", [])):
        raise ValueError(f"Duplicate numeric check in {run['run_id']}")
    for field, assertion in expected.items():
        check = numbers.get(field)
        if check is None and not assertion.get("required", True):
            continue
        if (
            not isinstance(check, dict)
            or not checked_pass(check)
            or "observed" not in check
            or not numeric_matches(check["observed"], assertion)
        ):
            reasons.append(f"Numeric field not evidenced or incorrect: {field}")
    for field in set(numbers) - set(expected):
        # Additional reported figures need independent receipts, not silent exemption.
        if not checked_pass(numbers[field]) or not numbers[field].get("oracle_ref"):
            reasons.append(f"Additional numeric claim not evidenced or incorrect: {field}")
    if not run.get("review_complete") or not run.get("evidence_refs"):
        reasons.append("Complete independent review evidence is unavailable")
    if run.get("critical_failures"):
        reasons.extend(f"Critical failure: {item}" for item in run["critical_failures"])
    action_expectations = required.get("state_assertions")
    if action_expectations:
        observation = run.get("state_observation", {})
        if not observation.get("evidence_refs"):
            reasons.append("Operational state read-back evidence is unavailable")
        for key in ("created_case_count", "status"):
            if observation.get(key) != action_expectations[key]:
                reasons.append(f"Operational state differs: {key}")
        if (
            action_expectations.get("required_readback")
            and observation.get("read_back") is not True
        ):
            reasons.append("Operational state was not read back")
    actual_state = run.get("business_state", "not_observed")
    if actual_state != required["expected_business_state"]:
        reasons.append(
            f"Business state: {actual_state}; expected {required['expected_business_state']}"
        )
    application_complete = (
        run.get("raw_status") == "completed" and run.get("application_verification") is True
    )
    return {
        "case_id": run["case_id"],
        "arm": run["arm"],
        "execution_phase": run.get("execution_phase", run["arm"]),
        "evidence_mode": run["evidence_mode"],
        "run_id": run["run_id"],
        "run_ids": run.get("run_ids", [run["run_id"]]),
        **(
            {
                "turn_progress": {
                    key: run[key]
                    for key in ("planned_turn_count", "observed_turn_count", "unrun_turn_indices")
                }
            }
            if "planned_turn_count" in run
            else {}
        ),
        "strict_pass": not reasons,
        "dimensions": dimensions,
        "business_state": actual_state,
        "failure_reasons": reasons,
        "raw_status": run.get("raw_status", "unavailable"),
        "failure_kind": run.get("failure_kind"),
        "application_verification": run.get("application_verification"),
        "application_complete": application_complete,
        "end_to_end_complete": not reasons and application_complete,
        "workflow_failure_kind": run.get("workflow_failure_kind"),
        "notes": run.get("notes", []),
        "models_observed": run.get("models_observed", []),
        "models_requested": run.get("models_requested", []),
        "source_identity": identity,
        "evidence_file_sha256": run.get("evidence_file_sha256", {}),
        "telemetry": run.get("telemetry", {}),
        "evidence_refs": run.get("evidence_refs", []),
    }


def summarize(protocol: dict, reviews: dict, path: Path = PROTOCOL) -> dict:
    if reviews.get("protocol_sha256") != digest(path):
        raise ValueError("Review protocol hash differs from the frozen file")
    raw_runs = reviews.get("runs", [])
    seen = set()
    for run in raw_runs:
        if run.get("evidence_mode") not in {"live", "fixture"}:
            raise ValueError("Every run must distinguish live from fixture evidence")
        key = (run["arm"], run["evidence_mode"], run["case_id"])
        if key in seen:
            raise ValueError("Externally repaired reruns need a distinct arm/version label")
        seen.add(key)
    scored = [score_run(protocol, run) for run in raw_runs]
    groups = defaultdict(list)
    for run in scored:
        groups[(run["arm"], run["evidence_mode"])].append(run)
    aggregates = []
    for (arm, mode), runs in groups.items():
        telemetry = {}
        for field in (
            "latency_seconds",
            "cost_usd",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "retries",
            "failed_tool_calls",
        ):
            values = [run["telemetry"].get(field) for run in runs]
            observed = [
                value
                for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            telemetry[field] = {
                "available_cases": len(observed),
                "unavailable_cases": len(values) - len(observed),
                "sum": sum(observed) if observed else None,
                "median": median(observed) if observed else None,
            }
        aggregates.append(
            {
                "arm": arm,
                "evidence_mode": mode,
                "attempted_cases": len(runs),
                "observed_run_count": len({run_id for run in runs for run_id in run["run_ids"]}),
                "strict_pass_cases": sum(run["strict_pass"] for run in runs),
                "strict_pass_fraction": sum(run["strict_pass"] for run in runs) / len(runs),
                "application_complete_cases": sum(run["application_complete"] for run in runs),
                "end_to_end_complete_cases": sum(run["end_to_end_complete"] for run in runs),
                "business_states": {
                    state: sum(run["business_state"] == state for run in runs)
                    for state in protocol["rubric"]["business_states"]
                },
                "unrun_case_ids": [
                    case["id"]
                    for case in protocol["cases"]
                    if case["id"] not in {run["case_id"] for run in runs}
                ],
                "telemetry_unit": "Case totals; multi-turn case latency and cost sum its distinct runs. Summed latency is not elapsed batch wall time.",
                "telemetry": telemetry,
            }
        )
    pairs = []
    for comparison in reviews.get("comparisons", []):
        baseline, candidate = comparison["baseline_arm"], comparison["candidate_arm"]
        mode = comparison.get("evidence_mode", "live")
        left = {run["case_id"]: run for run in groups.get((baseline, mode), [])}
        right = {run["case_id"]: run for run in groups.get((candidate, mode), [])}
        common = sorted(set(left) & set(right))
        pairs.append(
            {
                "baseline_arm": baseline,
                "candidate_arm": candidate,
                "evidence_mode": mode,
                "paired_case_ids": common,
                "baseline_strict_pass": sum(left[key]["strict_pass"] for key in common),
                "candidate_strict_pass": sum(right[key]["strict_pass"] for key in common),
                "paired_case_count": len(common),
                "unpaired_baseline_ids": sorted(set(left) - set(right)),
                "unpaired_candidate_ids": sorted(set(right) - set(left)),
            }
        )
    return {
        "protocol_sha256": digest(path),
        "runs": scored,
        "review_amendments": reviews.get("review_amendments", []),
        "aggregates": aggregates,
        "paired_comparisons": pairs,
        "interpretation": "Independent strict task success follows the frozen rubric. Application completion also requires observed completed status and verified QA; end-to-end completion requires both. Data-unavailable and human-work-pending states do not establish solved operational bottlenecks. Fixture results are not live-model evidence. Telemetry sums with missing runs are partial.",
    }


class ReportText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        elif tag in {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    public = commands.add_parser("public-input")
    public.add_argument("case_id")
    report = commands.add_parser("inspect-html")
    report.add_argument("path", type=Path)
    summary = commands.add_parser("summarize")
    summary.add_argument("review", type=Path)
    summary.add_argument("--output", type=Path)
    args = parser.parse_args()
    protocol = load_protocol()
    if args.command == "validate":
        result = validate(protocol)
    elif args.command == "public-input":
        result = public_input(protocol, args.case_id)
    elif args.command == "inspect-html":
        extract = ReportText()
        extract.feed(args.path.read_text(encoding="utf-8-sig"))
        content = "\n".join(
            line.strip() for line in "".join(extract.parts).splitlines() if line.strip()
        )
        result = {
            "path": str(args.path.resolve()),
            "sha256": digest(args.path),
            "text": content,
            "contains_claim_markers": "[claim:" in content,
            "verification_limit": "Text inspection only. Review question coverage and rendered appearance separately.",
        }
    else:
        result = summarize(protocol, json.loads(args.review.read_text(encoding="utf-8-sig")))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("valid") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
