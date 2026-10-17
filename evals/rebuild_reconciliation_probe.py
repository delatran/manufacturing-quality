"""Offline proof for evaluator-only terminal usage reconciliation. No network."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_rebuild_eval import (  # noqa: E402
    independent_price,
    now,
    retrieve_existing_usage,
    retrieved_terminal_usage,
    write_json,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **kwargs):
        return copy.deepcopy(self.payload)


class ReadOnlyFake:
    def __init__(self, payloads):
        self.responses = self
        self.payloads = payloads
        self.reads = []
        self.generations = 0

    async def retrieve(self, response_id):
        self.reads.append(response_id)
        value = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return FakeResponse(value)

    async def create(self, **kwargs):
        self.generations += 1
        raise AssertionError("The evaluator must not create a generation during reconciliation")


async def main():
    protocol = json.loads((ROOT / "evals/rebuild_protocol_v1.json").read_text(encoding="utf-8"))
    output = ROOT / ".runtime/rebuild-20261016/reconciliation-probe"
    output.mkdir(parents=True, exist_ok=True)
    harness = ROOT / "scripts/run_rebuild_eval.py"
    source_before = hashlib.sha256(harness.read_bytes()).hexdigest()
    checks = []

    def checked(name, condition):
        assert condition, name
        checks.append({"name": name, "status": "Passed"})

    terminal = {
        "id": "resp_synthetic_known",
        "model": "gpt-6-luna",
        "status": "cancelled",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 20},
            "output_tokens_details": {"reasoning_tokens": 7},
        },
        "output": [],
        "error": None,
    }
    row = retrieved_terminal_usage(terminal, protocol)
    checked(
        "exact_known_usage_matches_frozen_independent_price",
        independent_price(row, protocol) == row["cost_usd"],
    )
    checked("generation_elapsed_remains_unavailable", row["elapsed_seconds"] is None)
    absent = copy.deepcopy(terminal)
    absent["usage"]["input_tokens_details"] = {}
    unknown = retrieved_terminal_usage(absent, protocol)
    checked(
        "cache_unknown_is_not_zero",
        unknown["cached_tokens"] is None and unknown["cache_write_tokens"] is None,
    )
    checked(
        "cache_unknown_has_conservative_bound_and_no_exact_price",
        independent_price(unknown, protocol) is None
        and unknown["cost_basis"] == "conservative_missing_cache_details"
        and unknown["cost_upper_bound_usd"] >= row["cost_usd"],
    )
    for name, mutate in [
        ("nonterminal", lambda r: r.update(status="in_progress")),
        ("wrong_model", lambda r: r.update(model="unapproved-model")),
        ("boolean_total", lambda r: r["usage"].update(input_tokens=True)),
        (
            "inconsistent_cache",
            lambda r: r["usage"]["input_tokens_details"].update(cached_tokens=101),
        ),
        ("unauthorized_hosted_output", lambda r: r.update(output=[{"type": "web_search_call"}])),
    ]:
        raw = copy.deepcopy(terminal)
        mutate(raw)
        checked(name + "_cannot_settle", retrieved_terminal_usage(raw, protocol) is None)

    api = ReadOnlyFake([terminal])
    direct = await retrieve_existing_usage(api, {terminal["id"]}, output / "direct", protocol, 1)
    checked(
        "same_id_terminal_read_has_zero_generations",
        len(direct["records"]) == 1
        and not direct["unresolved_response_ids"]
        and api.reads == [terminal["id"]]
        and api.generations == 0,
    )

    active = copy.deepcopy(terminal)
    active.update(status="in_progress", usage=None)
    delayed_api = ReadOnlyFake([active, terminal])
    delayed = await retrieve_existing_usage(
        delayed_api, {terminal["id"]}, output / "delayed", protocol, 6
    )
    checked(
        "late_terminal_usage_can_settle_without_new_generation",
        len(delayed["records"]) == 1
        and not delayed["unresolved_response_ids"]
        and len(delayed_api.reads) == 2
        and set(delayed_api.reads) == {terminal["id"]}
        and delayed_api.generations == 0
        and delayed["elapsed_seconds"] < 6,
    )

    wrong_id = copy.deepcopy(terminal)
    wrong_id["id"] = "resp_synthetic_other"
    wrong_api = ReadOnlyFake([wrong_id])
    wrong = await retrieve_existing_usage(
        wrong_api, {terminal["id"]}, output / "wrong-id", protocol, 0.02
    )
    checked(
        "different_response_identity_keeps_reservation",
        not wrong["records"]
        and wrong["unresolved_response_ids"] == [terminal["id"]]
        and wrong_api.generations == 0,
    )

    no_usage = copy.deepcopy(terminal)
    no_usage["usage"] = None
    no_usage_api = ReadOnlyFake([no_usage])
    omitted = await retrieve_existing_usage(
        no_usage_api, {terminal["id"]}, output / "omitted", protocol, 0.02
    )
    checked(
        "missing_totals_remain_unresolved",
        not omitted["records"]
        and omitted["unresolved_response_ids"] == [terminal["id"]]
        and no_usage_api.generations == 0,
    )

    stopped = copy.deepcopy(terminal)
    stopped.update(status="failed", error={"code": "misalignment_policy_violation"})
    stop_api = ReadOnlyFake([stopped])
    stop = await retrieve_existing_usage(
        stop_api, {terminal["id"]}, output / "provider-stop", protocol, 1
    )
    checked(
        "late_provider_stop_is_preserved_even_with_known_usage",
        stop["provider_stop"] and len(stop["records"]) == 1 and stop_api.generations == 0,
    )
    incomplete_stop = copy.deepcopy(terminal)
    incomplete_stop.update(
        status="incomplete",
        error=None,
        incomplete_details={"reason": "misalignment_policy_violation"},
    )
    incomplete_api = ReadOnlyFake([incomplete_stop])
    incomplete = await retrieve_existing_usage(
        incomplete_api, {terminal["id"]}, output / "incomplete-stop", protocol, 1
    )
    checked(
        "late_policy_stop_in_incomplete_metadata_blocks_fresh_cases",
        incomplete["provider_stop"]
        and len(incomplete["records"]) == 1
        and incomplete_api.generations == 0,
    )

    empty_api = ReadOnlyFake([terminal])
    no_time = await retrieve_existing_usage(
        empty_api, {terminal["id"]}, output / "no-time", protocol, 0
    )
    checked(
        "no_remaining_allowance_means_no_provider_read",
        not empty_api.reads
        and not no_time["records"]
        and no_time["unresolved_response_ids"] == [terminal["id"]],
    )
    helper_source = inspect.getsource(retrieve_existing_usage)
    checked(
        "reconciliation_calls_retrieve_only",
        ".responses.retrieve(" in helper_source
        and ".responses.create(" not in helper_source
        and ".responses.cancel(" not in helper_source,
    )
    source_after = hashlib.sha256(harness.read_bytes()).hexdigest()
    checked("harness_unchanged_during_probe", source_before == source_after)
    receipt = {
        "observed_at_utc": now(),
        "evidence_mode": "offline_synthetic_fake_provider_only",
        "command": ".venv/Scripts/python.exe evals/rebuild_reconciliation_probe.py",
        "status": "Passed",
        "model_generations": 0,
        "network_requests": 0,
        "harness_sha256": source_after,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": checks,
        "delayed_read_elapsed_seconds": delayed["elapsed_seconds"],
        "limitations": [
            "Tests evaluator accounting instrumentation only, not a live Agent task.",
            "Actual application outcomes and deadlines are never changed by reconciliation.",
        ],
    }
    write_json(output / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "status": "Passed",
                "checks": len(checks),
                "model_generations": 0,
                "network_requests": 0,
                "receipt": str(output / "receipt.json"),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
