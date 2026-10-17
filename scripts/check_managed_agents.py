"""Check Agents API admission under the configured application profile.

The default is local inspection. --live submits one minimal managed turn against
the existing development ledger. Explicitly bounded profiles never retry
without their spending limit; the default unbounded profile omits that field.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import Agent, RunConfig, Runner  # noqa: E402
from analyst.config import Settings  # noqa: E402
from analyst.managed_agents import ManagedAgentsModel  # noqa: E402
from analyst.store import SessionStore  # noqa: E402


async def check(live: bool) -> int:
    settings = Settings.from_env(ROOT)
    settings.validate()
    summary = {
        "provider_api": "agents",
        "application_default": settings.agent_backend,
        "model": settings.model,
        "key_configured": bool(settings.api_key),
        "environment": "none",
        "native_subagents": False,
        "requested_session_limit_usd": settings.run_limit("max_cost_usd_per_run"),
        "unbounded_runs": settings.unbounded_runs,
        "live": live,
    }
    print(json.dumps(summary), flush=True)
    if not live:
        return 0
    if not settings.api_key:
        raise RuntimeError("A backend OPENAI_API_KEY is required")
    state_root = settings.root / settings.state_dir
    if not (state_root / "workspace.sqlite3").is_file():
        raise RuntimeError("The existing development ledger is required")
    store = SessionStore(state_root)
    try:
        pending = store.db.execute(
            "SELECT COUNT(*) FROM budget_reservations WHERE settled=0"
        ).fetchone()[0]
        if pending and not settings.unbounded_runs:
            raise RuntimeError("Reconcile existing unsettled reservations before this check")
        remaining = store.remaining_budget(settings.run_limit("dev_api_budget_usd"))
        if remaining is not None and remaining <= 0:
            raise RuntimeError("Existing development allocation cannot reserve this check")
        session = store.create_session()
        sid = session["id"]
        rid, folder = store.begin_run(sid, language="en")
        reservation = store.reserve_budget(
            rid,
            settings.run_limit("max_cost_usd_per_run"),
            settings.run_limit("dev_api_budget_usd"),
        )
        budget: dict = {"cost_usd": 0.0}

        async def emit(kind: str, data: dict) -> dict:
            return store.emit(sid, rid, kind, data)

        model = ManagedAgentsModel(settings, emit, budget=budget, session_budget_usd=reservation)
        result: dict = {
            **summary,
            "local_session_id": sid,
            "local_run_id": rid,
            "actual_cost_usd": None,
        }
        exit_code = 0
        try:
            async with asyncio.timeout(settings.run_limit("run_timeout_seconds")):
                outcome = await Runner.run(
                    Agent(
                        name="Managed API compatibility check",
                        instructions="Return only the exact requested word. Do not use tools.",
                        model=model,
                    ),
                    "Reply exactly READY.",
                    max_turns=settings.run_limit("max_turns"),
                    run_config=RunConfig(tracing_disabled=True),
                )
            result.update(
                status="completed" if outcome.final_output == "READY" else "unverified",
                answer=outcome.final_output,
                provider_session_id=model.session_id,
                usage=model.usage,
                session_limit_reservation_usd=reservation,
                reservation_retained=reservation is not None,
            )
        except asyncio.CancelledError:
            exit_code = 2
            result.update(
                status="cancelled",
                provider_session_id=model.session_id,
                reservation_retained=True,
                unresolved_generation_requests=budget.get("unresolved_generation_requests"),
                fallback_dispatched=False,
            )
            raise
        except Exception as error:
            rejected = budget.get("preflight_failure", {}).get("generation_dispatched") is False
            result.update(
                status="admission_rejected" if rejected else "failed",
                error_type=type(error).__name__,
                error=str(error),
                provider_session_id=model.session_id,
                generation_dispatched=False if rejected else "unconfirmed",
                reservation_retained=not rejected,
                fallback_dispatched=False,
            )
            exit_code = 2
            if rejected and budget.get("unresolved_generation_requests") == 0:
                result["accounted_cost_usd"] = 0.0
                result["reservation_retained"] = False
        finally:
            retained = float(budget.get("cost_usd") or 0)
            if not math.isfinite(retained) or retained < 0:
                retained = reservation or 0.0
            if retained:
                store.record_cost(rid, "managed-reservation:" + rid, retained)
            store.emit(sid, rid, "managed_compatibility_result", result)
            state = (
                "cancelled"
                if result.get("status") == "cancelled"
                else "failed"
                if exit_code
                else "unverified"
            )
            store.finish_run(rid, state, result)
            if result.get("reservation_retained") is False:
                store.settle_budget(rid)
            (folder / "managed_compatibility_result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            await model.client.close()
        print(
            json.dumps({"result": result, "evidence": str(folder)}, ensure_ascii=True), flush=True
        )
        return exit_code
    finally:
        store.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make one billable API admission check using the configured execution profile",
    )
    raise SystemExit(asyncio.run(check(parser.parse_args().live)))
