"""Run the frozen Vietnamese handover addendum through the immutable shared runner.

Validation is offline. Only ``run --live`` can dispatch an explicitly released
addendum; it never edits the main protocol, shared runner, or observer helper.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "evals/rebuild_bilingual_handover_protocol_v1.json"
EXPECTED_PROTOCOL = "e63fb2cb48483ea720ab45988be92457cf1f678a564cd841a08434e17075ef24"
EXPECTED_SHARED = {
    "scripts/run_rebuild_eval.py": "a915b4af691afdbd6fbbe48a1f0def8e34c9696fdc77238c8cb31db5e556252d",
    "scripts/eval_browser_observer.py": "b3bf400da081c39502dce68e670f8d9475b9b0c8fdb4fe683ffce5bfc57b0e39",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


def load_shared() -> ModuleType:
    if digest(PROTOCOL) != EXPECTED_PROTOCOL:
        raise ValueError("Frozen addendum protocol changed")
    for relative, expected in EXPECTED_SHARED.items():
        if digest(ROOT / relative) != expected:
            raise ValueError("Frozen shared evaluator changed: " + relative)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "rebuild_frozen_addendum_runner", ROOT / "scripts/run_rebuild_eval.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the frozen shared evaluator")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    shared_manifest = runner.evaluator_manifest

    def addendum_manifest() -> dict[str, str]:
        return {
            **shared_manifest(),
            Path(__file__).relative_to(ROOT).as_posix(): digest(Path(__file__)),
        }

    vars(runner).update(PROTOCOL=PROTOCOL, evaluator_manifest=addendum_manifest)
    runner.validate()
    return runner


def main_seal(suite_id: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", suite_id):
        raise ValueError("main-suite must be an existing simple suite identifier")
    path = ROOT / "evals" / ("rebuild_" + suite_id.replace("-", "_") + "_seal.json")
    seal = read_json(path)
    result = ROOT / seal["original_result"]
    if digest(result) != seal["original_result_sha256"]:
        raise ValueError("Sealed main result changed")
    if not seal.get("finished_at_utc") or seal.get("observed_cases") != 8:
        raise ValueError("The main eight-case attempt is not sealed")
    if seal.get("source_drift") or seal.get("evaluator_drift"):
        raise ValueError("Main identity drift must be resolved before a new release")
    return path, seal


async def run(args: argparse.Namespace, runner: ModuleType) -> None:
    if not args.live:
        raise SystemExit(
            "No generation dispatched. Explicit coordinator release and --live are required."
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.suite_id):
        raise ValueError("suite-id must be a fresh simple identifier")
    seal_path, seal = main_seal(args.main_suite)
    output = ROOT / ".runtime/rebuild-20261016/suites" / args.suite_id
    if output.exists():
        raise SystemExit("Suite exists; never replay or overwrite an addendum")
    current_source = runner.manifest()
    receipt = {
        "schema_version": 1,
        "suite_id": args.suite_id,
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Separate single-turn Vietnamese handover acceptance; excluded from main R01-R08 counts",
        "protocol_sha256": digest(PROTOCOL),
        "entrypoint_sha256": digest(Path(__file__)),
        "shared_evaluator_manifest": runner.evaluator_manifest(),
        "source_manifest": current_source,
        "sealed_main_suite": args.main_suite,
        "sealed_main_result_status": seal["status"],
        "main_seal_path": seal_path.relative_to(ROOT).as_posix(),
        "main_seal_sha256": digest(seal_path),
        "source_differs_from_main": current_source != seal["source_manifest_after"],
        "release_requirement": "Coordinator must explicitly release this invocation after fresh current source gates; --live does not authorize any later replay",
        "runner_adaptation": "Only the module-local protocol pointer and evaluator-source manifest are parameterized. Production app, SDK tools, limits, source oracle and shared execution code are unchanged.",
        "command": ".venv/Scripts/python.exe evals/rebuild_bilingual_handover.py run --live --suite-id "
        + args.suite_id
        + " --main-suite "
        + args.main_suite,
    }
    runner.write_json(ROOT / "evals" / ("rebuild_" + args.suite_id + "_dispatch.json"), receipt)
    await runner.run_live(
        argparse.Namespace(live=True, suite_id=args.suite_id, case=[], observe_ui=False)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "run"])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--suite-id", default="bilingual-vi-v1")
    parser.add_argument("--main-suite", default="post-v5")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    shared = load_shared()
    if args.command == "validate":
        print(
            json.dumps(
                {
                    "status": "Passed",
                    "protocol_sha256": digest(PROTOCOL),
                    "shared_evaluator": shared.evaluator_manifest(),
                    "generation_dispatched": False,
                },
                indent=2,
            )
        )
    else:
        asyncio.run(run(args, shared))
