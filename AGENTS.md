# Manufacturing quality workspace

- Setup: `./scripts/setup.ps1` on Windows, `bash scripts/setup.sh` on Linux/macOS.
- Run: `./scripts/start.ps1`; app serves the built React workspace and API together.
- Check: `.venv/Scripts/python.exe -m pytest tests -q`; `npm run build` in `frontend`.
- `analyst/` owns managed Agents API orchestration, the shared SDK model contract, local tools, evidence and memory.
- `analyst/dashboard.py` serves canonical case snapshots, scoped KPIs, records and evidence to both Dashboard and Agent; keep calculations in the Python backend.
- Dashboard opens by default. Agent prompts bind an immutable analysis context; changed scopes require a separate run and must not replace a running context.
- `knowledge/` holds the case rules and independent expected results; `config/` holds sourced model capabilities.
- Inputs and original documents are read-only. Execute code in per-run workspaces. Agent results stay in chat with registered inline charts; result exports are unavailable. Keep internal evidence files and the Dashboard's existing export controls.
- Reports require numeric claims tied to successful tool receipts and code hashes. Never present simulation as live model evidence.
- Use the managed OpenAI Agents API exclusively for local and Cloudflare execution (`agent_backend="agents_api"`). Alternate backend values are rejected. The shared `AgentModel` contract supports SDK interoperability without a separate provider transport. Preserve compaction items. Keep `MULTI_AGENT_NATIVE=false`.
- Paid model requests in this workspace must use `gpt-6-luna` only. Historical capability records do not authorize other models.
- The owner-authorized local and Cloudflare profile is `unbounded_runs=true`: use `Settings.run_limit(name)` so no per-question cost ceiling, development budget ceiling, output token cap, turn cap, tool-call cap or application deadline applies. Keep finite values only for explicit bounded fixtures. Managed sessions omit `spend_control`; serialize absent application limits as null, never infinity. Preserve spending history and unresolved reservations. Respect owner cancellation, provider stop signals and infrastructure constraints. Verify the deployed profile and actual managed session behavior before claiming production support.
- Do not print or commit secrets. Unbounded execution does not grant external action permissions or allow original input files to be changed.
- Long-term memory stores schemas, rules, caveats and preferences, never raw data rows.
- Local source edits, UI tests and real OpenAI API execution for the current Agent verification are authorized by the owner. Ask before changing original data or publishing unless the current request separately authorizes that target and effect.
