"""Product capabilities over the same immutable analysis service as the UI."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import jsonschema

from .analysis_sql import execute_snapshot_query
from .case_study.analytics import Scope
from .case_study.models import Event, Product
from .claim_feedback import field_feedback
from .dashboard import SORT_FIELDS
from .evidence import canonical, sha256
from .report_output import unit_note
from .tools import ToolEnvelope as ToolEnvelope
from .tools import function_schema

TOOLS_VERSION = "dashboard-analysis-v15"
INSTRUCTIONS_VERSION = "investigation-v16"
SCENARIOS = ["", "exclude_cost_10", "ambiguous_dm", "year_2024"]
SECTIONS = [
    "overview",
    "schema",
    "kpis",
    "monthly",
    "trend",
    "period_halves",
    "lines",
    "line_category",
    "category_summary",
    "shared_category_controls",
    "line_cost_comparison",
    "line_comparison_note",
    "products",
    "location_severity",
    "costs",
    "cost_distribution",
    "quality",
    "anomalies",
    "recommendations",
    "definitions",
    "calendar_coverage",
    "decomposition",
    "cleaning_impact",
    "inspection_methods",
    "top_product_cluster",
    "event_populations",
    "metadata",
    "quality_scope",
    "metric_metadata",
    "scope",
]
PANELS = ["dashboard", "records", "evidence", "investigation", "brief"]


class InvestigationEvidenceError(ValueError):
    def __init__(self, failures):
        self.fields = [item["field"] for item in failures]
        self.details: list[dict[str, Any]] = []
        for item in failures:
            if len(canonical([*self.details, item]).encode("utf-8")) <= 8000:
                self.details.append(item)
        message = "; ".join(
            item["field"] + ": " + "; ".join(item["errors"][:3]) for item in failures[:4]
        )
        super().__init__("Investigation evidence failed; no fields saved. " + message)


def scope_schema():
    return {
        "type": "object",
        "properties": {item.name: {"type": ["string", "null"]} for item in fields(Scope)},
        "required": [item.name for item in fields(Scope)],
        "additionalProperties": False,
    }


def product_schemas():
    text = {"type": "string", "maxLength": 6000}
    text_list = {"type": "array", "items": text, "maxItems": 12}
    plan = {
        "type": ["string", "null"],
        "maxLength": 600,
        "description": "One concise visible plan in the selected response language for a multi-step investigation, included with its first useful analysis call. Null for simple questions or later calls. Never include private reasoning.",
    }
    rationale = {
        "type": ["string", "null"],
        "description": "One concise public sentence in the selected response language explaining why this tool is chosen and which analytical hypothesis or decision question it answers. Null when no explanation helps; never include private reasoning.",
    }
    hypothesis = {
        "type": "object",
        "properties": {
            "id": {"type": ["string", "null"]},
            "text": text,
            "status": {"type": "string", "enum": ["open", "rejected", "supported", "unresolved"]},
        },
        "required": ["id", "text", "status"],
        "additionalProperties": False,
    }
    priority = {
        "type": "object",
        "properties": {name: text for name in ["evidence", "action", "owner", "metric"]},
        "required": ["evidence", "action", "owner", "metric"],
        "additionalProperties": False,
    }
    return [
        function_schema(
            "analysis_query",
            "Read standard KPIs and report sections in the current scope or a labelled alternative scope. Primary tool for standard metrics, aggregations, and predefined sections (overview, kpis, trend, lines, costs, etc.). Null filters preserve the run scope. Quality and cleaning_impact always use the full canonical snapshot and explicitly disclose that event filters and scenarios do not apply. Backend computes every metric. Use schema for columns and options. Collections include a small preview and exact next_call for optional detail, not a requirement to read every row. Do not use analysis_sql when analysis_query provides the section.",
            {
                "section": {"type": "string", "enum": SECTIONS},
                "filters": scope_schema(),
                "plan": plan,
                "rationale": rationale,
            },
        ),
        function_schema(
            "analysis_compare",
            "Compare two periods or scopes and decompose their event-count difference by one dimension. Use this tool specifically for period-over-period or scope-to-scope variance decomposition (by production_line, category, defect_type, etc.). Can be the first data call: the server verifies the frozen context. Returns period totals, central difference/reconciliation and a ranked contribution preview. Contributions describe changes, not causes. Change dimension only to test a relevant competing explanation; do not page the entire cohort for a reconciled summary.",
            {
                "baseline_filters": scope_schema(),
                "comparison_filters": scope_schema(),
                "dimension": {
                    "type": "string",
                    "enum": [
                        "production_line",
                        "category",
                        "product_id",
                        "defect_type",
                        "defect_location",
                        "severity",
                        "inspection_method",
                    ],
                },
                "plan": plan,
                "rationale": rationale,
            },
        ),
        function_schema(
            "scenario_compare",
            "Compute the selected assumption against canonical on the same dataset and filters. Use this tool exclusively to evaluate predefined sensitivity scenarios (exclude_cost_10, ambiguous_dm, year_2024); do not use for ad-hoc filtering. exclude_cost_10 removes those values from financial availability only; ambiguous_dm changes ambiguous-date parsing; year_2024 hypothesizes correcting out-of-period years. Canonical data is never changed. Use ui_command to display the scenario only when the user asks to try it.",
            {
                "scenario": {"type": "string", "enum": SCENARIOS},
                "plan": plan,
                "rationale": rationale,
            },
        ),
        function_schema(
            "analysis_records",
            "Page retained records for explicit filters on the frozen dataset. Use this tool to inspect individual record details, sample defects, or drill into specific defect dates; do not use to compute cohort-level aggregates. Null preserves the immutable run base, never follows UI implicitly. Use defect_date to drill into an exact anomaly day without changing the monthly analysis scope; null includes all selected days. Repeat query filters to inspect its evidence. Source row identities link to analysis_provenance. Unknown repair costs stay null.",
            {
                "page": {"type": "integer", "minimum": 1},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
                "sort_by": {
                    "type": "string",
                    "enum": sorted(SORT_FIELDS),
                },
                "sort_order": {"type": "string", "enum": ["asc", "desc"]},
                "defect_date": {
                    "type": ["string", "null"],
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
                "filters": scope_schema(),
                "rationale": rationale,
            },
        ),
        function_schema(
            "analysis_provenance",
            "Read source identity and normalization audit for one defect ID or original source row, including quarantined/out-of-period variants. Use this tool for tracing ingestion provenance, data cleaning rules, and audit logs of individual records; do not use for standard KPI reporting or aggregate comparisons. Choose only one selector. Both null reads source schema, full-source quality and cleaning rules. Provenance does not imply that every audited row belongs to the selected event scope.",
            {
                "defect_id": {"type": ["integer", "null"], "minimum": 1},
                "source_row": {"type": ["integer", "null"], "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "filters": scope_schema(),
                "rationale": rationale,
            },
        ),
        function_schema(
            "ui_command",
            "Open a product panel, apply requested filters/scenario with undo, or undo a command. A pure view or undo request does not require rewriting the investigation. Use only when the user asks to change the view or try an assumption. Commands use compare-and-swap against the current UI scope and never replace the running analysis snapshot. Null filter values preserve the run scope. command_id is required only for undo.",
            {
                "action": {"type": "string", "enum": ["open", "apply_scope", "undo"]},
                "panel": {"type": "string", "enum": PANELS},
                "filters": scope_schema(),
                "command_id": {"type": ["string", "null"]},
            },
        ),
        function_schema(
            "investigation_update",
            "Persist an editable investigation separately from chat: findings, competing hypotheses, proposed priorities and next steps. Numeric evidence is checked before saving; interpretation awaits final semantic QA. Every numeric statement must use current-run [value:ID] or [claim:ID] evidence markers. The first successful save in each new run requires all six authored fields and current-run numeric markers, even when previous saved work exists. Empty collections use []. Previous-run fields and markers cannot be imported through null. Only after a successful save in this same run and exact scope, null authored fields preserve that annotated proposal. A non-null array replaces that whole array: copy unchanged elements exactly; [] clears it. Change only the fields needing repair and reuse the original evidence IDs. Owner is a proposed role; metrics needing future data must say so. Save once the requested investigation has sufficient evidence; avoid repeated planning or exhaustive cohort paging.",
            {
                "question": {**text, "type": ["string", "null"]},
                "summary": {**text, "type": ["string", "null"]},
                "findings": {**text_list, "type": ["array", "null"]},
                "hypotheses": {"type": ["array", "null"], "items": hypothesis, "maxItems": 12},
                "next_steps": {**text_list, "type": ["array", "null"]},
                "priorities": {"type": ["array", "null"], "items": priority, "maxItems": 6},
                "filters": scope_schema(),
            },
        ),
        function_schema(
            "analysis_sql",
            "Run a custom read-only DuckDB SELECT over events and products in an explicit frozen Dashboard scope. Use analysis_sql ONLY for custom cross-table joins, complex CTEs, window functions, or statistical aggregates not available through analysis_query. Use analysis_query for standard KPIs and report sections. Null filters preserve the run scope. events contains canonical retained event columns; products contains the selected reference product catalog. Discover columns with SELECT * FROM events LIMIT 0 and SELECT * FROM products LIMIT 0. Original inputs are read-only; external files, extensions and writes are unavailable. SQL and full results are retained as registered artifacts and evidence. These are analyst-derived results, so disclose assumptions and distinguish association from cause.",
            {
                "query": {"type": "string", "minLength": 1},
                "filters": scope_schema(),
                "rationale": rationale,
            },
        ),
    ]


class ProductTools:
    def __init__(self, runtime):
        self.runtime = runtime
        self.schemas = product_schemas()
        self.pending_investigation = None
        self.investigation_baseline: str | None = None
        if runtime.state_store is not None:
            self.bind_investigation_state(
                runtime.state_store.investigation(runtime.toolkit.session_id)
            )

    def bind_investigation_state(self, state):
        self.investigation_baseline = investigation_content_hash(state)

    async def call(self, name, arguments):
        if name == "analysis_records":
            arguments = {"defect_date": None, **arguments}
        if name == "analysis_provenance":
            arguments = {"source_row": None, "limit": 20, **arguments}
        if (
            name in {"analysis_query", "analysis_compare", "scenario_compare"}
            and "plan" not in arguments
        ):
            arguments = {**arguments, "plan": None}
        if (
            name
            in {
                "analysis_query",
                "analysis_compare",
                "scenario_compare",
                "analysis_records",
                "analysis_provenance",
                "analysis_sql",
            }
            and "rationale" not in arguments
        ):
            arguments = {**arguments, "rationale": None}
        if (
            name
            in {
                "investigation_update",
                "analysis_records",
                "analysis_provenance",
                "analysis_sql",
            }
            and "filters" not in arguments
        ):
            # Compatibility for internal callers predating explicit analysis scope.
            # The advertised strict SDK schema always requires this field.
            arguments = {**arguments, "filters": {field.name: None for field in fields(Scope)}}
        schema = next((schema for schema in self.schemas if schema["name"] == name), None)
        if schema is None:
            raise ValueError("Unsupported product tool")
        jsonschema.validate(arguments, schema["parameters"])
        runtime = self.runtime
        if runtime.analysis_service is None or runtime.analysis_context is None:
            raise ValueError("Select a valid dashboard snapshot before using product tools")
        if arguments.get("plan"):
            runtime.toolkit.todos = [{"title": arguments["plan"], "status": "in_progress"}]
            await runtime.emit("todo", {"items": runtime.toolkit.todos})
        bound = await runtime.read_dashboard_context()
        if runtime.context_entry is None:
            runtime.context_entry = runtime.import_dashboard_receipt(bound, "dashboard_context")
        context = bound["context"]
        if name == "analysis_query":
            filters = (
                arguments["filters"]
                if any(value is not None for value in arguments["filters"].values())
                else None
            )
            payload = await runtime.read_dashboard_context(filters)
            requested_context = payload["context"]
            section = arguments["section"]
            global_quality = section in {"quality", "cleaning_impact", "quality_scope"}
            if global_quality:
                payload = await runtime.read_dashboard_context(asdict(Scope()))
            selected = payload["analysis"]
            if section == "schema":
                content = {
                    "event_columns": [item.name for item in fields(Event)],
                    "product_columns": [item.name for item in fields(Product)],
                    "filter_options": payload.get("options", {}),
                    "available_sections": sorted(SECTIONS),
                    "source_schema_note": "No unit ID, process step, shift, lot, inspection exposure or actual monthly production is present.",
                }
            elif section == "overview":
                content = {
                    key: selected[key]
                    for key in ("kpis", "trend", "calendar_coverage")
                    if key in selected
                }
            elif section == "costs":
                content = {
                    "kpis": {
                        key: selected["kpis"][key]
                        for key in (
                            "event_count",
                            "known_repair_cost",
                            "cost_known_count",
                            "cost_unknown_count",
                            "cost_coverage_pct",
                            "cost_available",
                            "cost_status",
                        )
                    },
                    "costs": selected["costs"],
                }
            elif section not in selected:
                raise ValueError("This report does not provide section " + section)
            else:
                content = {section: selected[section]}
            if section in {
                "lines",
                "line_category",
                "category_summary",
                "shared_category_controls",
                "line_cost_comparison",
            }:
                content["line_comparison_note"] = selected["line_comparison_note"]
            return {
                "ok": True,
                "analysis": content,
                "context": payload["context"],
                "definitions": selected.get("definitions", {}),
                "metric_metadata": selected.get("metric_metadata", {}),
                "limitations": selected.get("metadata", {}).get("limitations", []),
                "links": payload.get("links", {}),
                "scope_semantics": {
                    "kind": "canonical_full_snapshot" if global_quality else "selected_scope",
                    "filters_applied": not global_quality,
                    "scenario_applied": bool(payload["context"].get("scenario"))
                    and not global_quality,
                    "requested_context_id": requested_context["context_id"],
                    "requested_filters": requested_context["filters"],
                    **(
                        {
                            "scope_total_path": "$.analysis.kpis",
                            "grouped_detail_path": "$.analysis.costs",
                            "grain_note": "kpis describe all logged defect events in the returned context; costs rows describe their named groups within that context.",
                        }
                        if section == "costs"
                        else {}
                    ),
                },
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, payload["context"]
        if name == "analysis_compare":
            result = await runtime.analysis_service.compare_periods(
                context["context_id"],
                {
                    key: value
                    for key, value in arguments["baseline_filters"].items()
                    if value is not None
                },
                {
                    key: value
                    for key, value in arguments["comparison_filters"].items()
                    if value is not None
                },
                arguments["dimension"],
            )
            return {
                "ok": True,
                "baseline": compact_context(result["baseline"]),
                "comparison": compact_context(result["comparison"]),
                "decomposition": result["decomposition"],
                "source_status": result.get("status", {}),
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, context
        if name == "scenario_compare":
            result = await runtime.analysis_service.compare_scenario(
                context["context_id"], arguments["scenario"]
            )
            return {
                "ok": True,
                "canonical": compact_context(result["canonical"]),
                "selected": compact_context(result["selected"]),
                "comparison": result["comparison"],
                "source_status": result.get("status", {}),
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, context
        if name == "analysis_records":
            target = await runtime.read_dashboard_context(
                arguments["filters"]
                if any(value is not None for value in arguments["filters"].values())
                else None
            )
            return {
                "ok": True,
                **await runtime.analysis_service.records(
                    target["context"]["context_id"],
                    **{
                        key: value
                        for key, value in arguments.items()
                        if key not in {"filters", "rationale"}
                    },
                ),
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, target["context"]
        if name == "analysis_provenance":
            target = await runtime.read_dashboard_context(
                arguments["filters"]
                if any(value is not None for value in arguments["filters"].values())
                else None
            )
            context = target["context"]
            if arguments["defect_id"] is not None or arguments["source_row"] is not None:
                return {
                    "ok": True,
                    **await runtime.analysis_service.trace_records(
                        context["context_id"],
                        defect_id=arguments["defect_id"],
                        source_row=arguments["source_row"],
                        limit=arguments["limit"],
                    ),
                    **(
                        {"rationale": arguments["rationale"]}
                        if arguments.get("rationale") is not None
                        else {}
                    ),
                }, context
            snapshot = await runtime.analysis_service.snapshot_for_context(context["context_id"])
            defect_id = arguments["defect_id"]
            audit = [
                asdict(entry)
                for entry in snapshot.audit
                if defect_id is not None and entry.record_id == str(defect_id)
            ]
            retained = [
                event.as_record() for event in snapshot.events if event.defect_id == defect_id
            ]
            return {
                "ok": True,
                "source": snapshot.provenance,
                "quality_scope": bound.get("quality_scope"),
                "defect_id": defect_id,
                "audit": audit[:100],
                "audit_truncated": len(audit) > 100,
                "canonical_retained_event": retained,
                "quality": snapshot.quality if defect_id is None else None,
                "interpretation": "Audit covers the complete source before current filters. Retained events are canonical; scenario assumptions are separately disclosed in the scope.",
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, context
        if name == "ui_command":
            if runtime.ui_handler is None:
                raise ValueError("UI commands are unavailable in this execution context")
            return {"ok": True, **await runtime.ui_handler(arguments)}, context
        if name == "investigation_update":
            target = await runtime.read_dashboard_context(
                arguments["filters"]
                if any(value is not None for value in arguments["filters"].values())
                else None
            )
            return await self.update_investigation(arguments, target["context"]), target["context"]
        if name == "analysis_sql":
            target = await runtime.read_dashboard_context(
                arguments["filters"]
                if any(value is not None for value in arguments["filters"].values())
                else None
            )
            target_context = target["context"]
            paths = await runtime.analysis_service.input_paths_for_context(
                target_context["context_id"]
            )
            query = arguments["query"]
            result = await execute_snapshot_query(query, paths, target["evidence"]["input_sha256"])
            identity = sha256(
                canonical({"query": query, "context": target_context, "result": result})
            )
            directory = Path(runtime.toolkit.run_dir) / "artifacts"
            directory.mkdir(parents=True, exist_ok=True)
            sql_retained = {"query": query, "context": target_context, **result}
            artifacts = []
            for extension, artifact_text in (
                ("sql", query),
                ("json", json.dumps(sql_retained, ensure_ascii=False, indent=2)),
            ):
                path = directory / f"snapshot-query-{identity}.{extension}"
                if path.exists() and path.read_text(encoding="utf-8") != artifact_text:
                    raise ValueError("A retained SQL artifact differs; refusing to overwrite it")
                path.write_text(artifact_text, encoding="utf-8")
                artifacts.append(
                    runtime.toolkit.ledger.register_artifact(
                        path, kind="snapshot_sql", context=target_context, locale=runtime.language
                    )
                )
            return {
                "ok": True,
                **sql_retained,
                "query_sha256": sha256(query),
                "artifacts": artifacts,
                "limitations": target["analysis"].get("metadata", {}).get("limitations", []),
                "interpretation": "Custom SQL computed from the exact selected canonical inputs. Query expressions define these results; they do not establish causal effects, historical exposure or canonical KPI equivalence.",
                **(
                    {"rationale": arguments["rationale"]}
                    if arguments.get("rationale") is not None
                    else {}
                ),
            }, target_context
        raise ValueError("Unsupported product tool")

    async def update_investigation(self, arguments, context):
        runtime = self.runtime
        if runtime.state_store is None:
            raise ValueError("Persistent investigation state is unavailable")
        from .runtime import ReportDraft, audit_field_reports, clean_draft_text

        current = runtime.state_store.investigation(runtime.toolkit.session_id)
        if investigation_content_hash(current) != self.investigation_baseline:
            raise ValueError(
                "The investigation was edited after this run read it; those edits were preserved. "
                "Start a new prompt to continue from the current saved content."
            )
        ui_context = current["ui"].get("context_id")
        if (
            ui_context is not None
            and ui_context != context["context_id"]
            and current["ui"].get("last_command_id") is None
        ):
            raise ValueError(
                "The user changed scope during this run; its old investigation cannot overwrite the new view"
            )
        authored_fields = (
            "question",
            "summary",
            "findings",
            "hypotheses",
            "next_steps",
            "priorities",
        )
        preserved = [field for field in authored_fields if arguments[field] is None]
        if preserved:
            pending = self.pending_state()
            if (
                pending is None
                or pending.get("run_id") != Path(runtime.toolkit.run_dir).name
                or pending.get("run_dir") != str(Path(runtime.toolkit.run_dir).resolve())
                or sha256(canonical(pending["state"])) != pending["state_sha256"]
                or pending["state"]["context_id"] != context["context_id"]
                or pending["state"]["scope"] != context["filters"]
                or not isinstance(pending.get("proposal"), dict)
                or pending.get("proposal_sha256") != sha256(canonical(pending["proposal"]))
            ):
                raise ValueError(
                    "Null authored fields require the unchanged, sealed investigation proposal "
                    "from a successful save in this same run and exact scope"
                )
            proposal = pending["proposal"]
            arguments = {**arguments, **{field: proposal[field] for field in preserved}}
            if all(arguments[field] == proposal[field] for field in authored_fields):
                raise ValueError("No investigation fields changed; do not repeat an unchanged save")
        texts = [("summary", arguments["summary"])]
        texts += [(f"findings[{index}]", text) for index, text in enumerate(arguments["findings"])]
        texts += [
            (f"hypotheses[{index}].text", item["text"])
            for index, item in enumerate(arguments["hypotheses"])
        ]
        texts += [
            (f"next_steps[{index}]", text) for index, text in enumerate(arguments["next_steps"])
        ]
        texts += [
            (f"priorities[{index}].{key}", text)
            for index, item in enumerate(arguments["priorities"])
            for key, text in item.items()
        ]
        entries = {entry["execution_id"]: entry for entry in runtime.toolkit.ledger.records()}
        available_claims = {
            identity: claim.model_dump() for identity, claim in runtime.available_claims().items()
        }
        checked_values = {}
        failures = []
        reports = {field: runtime.resolve_report(ReportDraft(report=text)) for field, text in texts}
        audits = audit_field_reports(reports, runtime.toolkit.ledger)
        for field, text in texts:
            report = reports[field]
            audit = audits[field]
            if not audit["verified"]:
                failures.append(field_feedback(field, text, audit, available_claims, entries))
            checked_values[field] = (
                clean_draft_text(report.report),
                [claim.model_dump() for claim in report.claims],
            )
        if failures:
            raise InvestigationEvidenceError(failures)
        findings = []
        for index, _text in enumerate(arguments["findings"]):
            clean, evidence = checked_values[f"findings[{index}]"]
            findings.append(
                {
                    "id": sha256(clean)[:16],
                    "text": clean,
                    "status": "unverified",
                    "evidence": evidence,
                    "numeric_claims_checked": True,
                    "context_id": context["context_id"],
                }
            )
        hypotheses = [
            {
                **item,
                "id": item["id"] or sha256(item["text"])[:16],
                "text": checked_values[f"hypotheses[{index}].text"][0],
            }
            for index, item in enumerate(arguments["hypotheses"])
        ]
        priorities = [
            {
                "id": sha256(canonical(item))[:16],
                **{key: checked_values[f"priorities[{index}].{key}"][0] for key in item},
            }
            for index, item in enumerate(arguments["priorities"])
        ]
        changes = {
            "question": arguments["question"],
            "summary": checked_values["summary"][0],
            "findings": findings,
            "hypotheses": hypotheses,
            "priorities": priorities,
            "next_steps": [
                checked_values[f"next_steps[{index}]"][0]
                for index in range(len(arguments["next_steps"]))
            ],
            "context_id": context["context_id"],
            "scope": context["filters"],
            "dataset_version": context.get("dataset_version", context["snapshot_id"]),
            "policy_version": context.get("policy_version"),
            "instructions_version": INSTRUCTIONS_VERSION,
            "tools_version": TOOLS_VERSION,
            "unit_notes": {
                language: note.partition("** ")[2]
                for language in ("en", "vi")
                if (
                    note := unit_note(
                        [claim for _, claims in checked_values.values() for claim in claims],
                        runtime.toolkit.ledger,
                        language,
                    )
                )
            },
        }
        changes.update(content_status="unverified", semantic_review=None)
        saved = runtime.state_store.save_investigation(
            runtime.toolkit.session_id, current["version"], changes
        )
        self.bind_investigation_state(saved)
        # This registered JSON is a read-back of work state, not an executable trace.
        directory = Path(runtime.toolkit.run_dir) / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"investigation-v{saved['version']}.json"
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = runtime.toolkit.ledger.register_artifact(
            path, kind="investigation", context=context, locale=runtime.language
        )
        proposal_json = canonical(arguments)
        self.pending_investigation = {
            "version": saved["version"],
            "state_sha256": sha256(canonical(saved)),
            "state": saved,
            "proposal": json.loads(proposal_json),
            "proposal_sha256": sha256(proposal_json),
            "run_id": Path(runtime.toolkit.run_dir).name,
            "run_dir": str(Path(runtime.toolkit.run_dir).resolve()),
            "artifact": artifact,
        }
        await runtime.emit("investigation", saved)
        return {
            "ok": True,
            "investigation": saved,
            "artifact": artifact,
            "validation": "Numeric claims checked; qualitative statements await final semantic review",
            "effect": "Local editable investigation draft saved and read back",
        }

    def pending_state(self):
        if self.pending_investigation is None or self.runtime.state_store is None:
            return None
        current = self.runtime.state_store.investigation(self.runtime.toolkit.session_id)
        if (
            current["version"] != self.pending_investigation["version"]
            or sha256(canonical(current)) != self.pending_investigation["state_sha256"]
        ):
            return None
        return self.pending_investigation

    async def review_investigation(self, review_receipt):
        pending = self.pending_state()
        if pending is None:
            return None
        runtime = self.runtime
        current = pending["state"]
        result = review_receipt.get("result", {})
        if (
            review_receipt.get("tool") != "report_semantic_review"
            or result.get("phase") != "reviewed"
            or result.get("qa_verdict", {}).get("verified") is not True
        ):
            raise ValueError(
                "Investigation promotion requires a successful semantic review receipt"
            )
        path = (Path(runtime.toolkit.run_dir) / result["runner_input_path"]).resolve()
        if (
            not path.is_relative_to((Path(runtime.toolkit.run_dir) / "internal").resolve())
            or sha256(path.read_bytes()) != result["runner_input_sha256"]
        ):
            raise ValueError("Investigation review input failed its retained seal")
        reviewed_input = json.loads(path.read_text(encoding="utf-8"))
        if (
            reviewed_input.get("investigation_draft", {}).get("state_sha256")
            != pending["state_sha256"]
        ):
            raise ValueError("Semantic review did not cover this exact investigation state")
        target = await runtime.read_dashboard_context(current["scope"])
        context = target["context"]
        if context["context_id"] != current["context_id"]:
            raise ValueError("The reviewed investigation context differs from its frozen scope")
        findings = [
            {**item, "status": "verified" if item["evidence"] else "reviewed"}
            for item in current["findings"]
        ]
        semantic_review = {
            "state_sha256": pending["state_sha256"],
            "reviewed_version": current["version"],
            "review_execution_id": review_receipt["execution_id"],
            "review_entry_sha256": review_receipt["entry_sha256"],
            "scope": current["scope"],
            "interpretation": "Numeric claims were checked against backend receipts; the exact content passed model semantic review.",
        }
        saved = runtime.state_store.save_investigation(
            runtime.toolkit.session_id,
            current["version"],
            {
                "findings": findings,
                "content_status": "reviewed",
                "semantic_review": semantic_review,
            },
        )
        path = (
            Path(runtime.toolkit.run_dir) / "artifacts" / f"investigation-v{saved['version']}.json"
        )
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        artifact = runtime.toolkit.ledger.register_artifact(
            path, kind="investigation", context=context, locale=runtime.language
        )
        await runtime.emit("investigation", saved)
        await runtime.emit("artifact", artifact)
        self.pending_investigation = None
        return {"draft_artifact": pending["artifact"], "reviewed_artifact": artifact}


def tool_code() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path(__file__),
            Path(__file__).with_name("analysis_sql.py"),
            Path(__file__).with_name("chart_views.py"),
            Path(__file__).resolve().parents[1] / "knowledge" / "glossary.json",
        )
    )


def investigation_content_hash(state):
    """Track owner content, excluding server UI, artifact and review bookkeeping."""
    content = {key: state.get(key) for key in ("question", "summary", "next_steps")}
    for key, names in (
        ("findings", ("id", "text")),
        ("hypotheses", ("id", "text", "status")),
        ("priorities", ("evidence", "action", "owner", "metric")),
    ):
        content[key] = [{name: item.get(name) for name in names} for item in state.get(key, [])]
    if any(state.get("unit_notes", {}).values()):
        content["unit_notes"] = state["unit_notes"]
    return sha256(canonical(content))


def compact_context(payload):
    return {
        "context": payload["context"],
        "analysis": payload["analysis"],
        "links": payload.get("links", {}),
        "canonical_receipt": {
            key: payload["evidence"][key]
            for key in ("execution_id", "code_sha256", "result_sha256", "entry_sha256")
        },
    }
