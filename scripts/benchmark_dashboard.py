"""Measure a fixed real-HTTP dashboard workload and labelled failure probes."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def measure() -> dict[str, Any]:
    from analyst.case_study.ingestion import SourceError
    from analyst.case_study.preparation import load_snapshot
    from analyst.dashboard import AnalysisService

    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rfid-dashboard-benchmark-") as directory:
        source_unavailable = False

        def controlled_source():
            if source_unavailable:
                raise SourceError("Deliberate benchmark source-failure probe")
            return load_snapshot(ROOT / "data/master_data.csv")

        service = AnalysisService(ROOT, directory, snapshot_loader=controlled_source)
        scopes = [
            ("cold_http_and_analysis", {}),
            ("warm_same_scope", {}),
            ("changed_line", {"production_line": "Line-3"}),
            ("changed_event_filter", {"production_line": "Line-3", "severity": "Critical"}),
            ("changed_scenario", {"scenario": "exclude_cost_10"}),
            ("empty_scope", {"production_line": "Line-3", "category": "Category 1"}),
        ]
        for name, filters in scopes:
            started = time.perf_counter()
            result = await service.get_context(filters)
            samples.append(
                {
                    "workload": name,
                    "elapsed_seconds": time.perf_counter() - started,
                    "status": "Passed",
                    "filters": result["context"]["filters"],
                    "context_id": result["context"]["context_id"],
                    "event_count": result["analysis"]["kpis"]["event_count"],
                    "model_calls": 0,
                }
            )
        initial = await service.get_context()

        def unavailable_source():
            raise SourceError("Deliberate benchmark source-failure probe")

        source_unavailable = True
        started = time.perf_counter()
        stale = await service.refresh()
        retained = (
            stale["status"]["stale"] is True
            and stale["context"]["context_id"] == initial["context"]["context_id"]
        )
        samples.append(
            {
                "workload": "source_failure_with_existing_snapshot",
                "elapsed_seconds": time.perf_counter() - started,
                "status": "Passed" if retained else "Failed",
                "probe": "Injected SourceError after a successful real HTTP fetch",
                "stale": stale["status"],
                "model_calls": 0,
            }
        )
        empty_service = AnalysisService(
            ROOT, Path(directory) / "empty", snapshot_loader=unavailable_source
        )
        started = time.perf_counter()
        try:
            await empty_service.get_context()
        except SourceError as error:
            samples.append(
                {
                    "workload": "source_failure_without_snapshot",
                    "elapsed_seconds": time.perf_counter() - started,
                    "status": "Passed",
                    "probe": "Injected SourceError with no prior snapshot",
                    "error": str(error),
                    "model_calls": 0,
                }
            )
        else:
            samples.append({"workload": "source_failure_without_snapshot", "status": "Failed"})
        return {
            "recorded_at": datetime.now(UTC).isoformat(),
            "description": "One observation per fixed local workload; timings are descriptive, not a benchmark of general performance.",
            "source": initial["analysis"]["metadata"],
            "samples": samples,
            "sample_count": len(samples),
            "model_cost_usd": 0,
            "status": "Passed"
            if all(sample["status"] == "Passed" for sample in samples)
            else "Failed",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/evidence/dashboard-performance.json"
    )
    args = parser.parse_args()
    result = asyncio.run(measure())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "sample_count": result["sample_count"],
                "output": str(args.output),
            }
        )
    )
    return 0 if result["status"] == "Passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
