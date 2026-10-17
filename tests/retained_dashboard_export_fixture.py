"""Create sealed historical Dashboard exports for retained-evidence audit tests.

This helper never advertises or dispatches an Agent tool. It deliberately uses
the legacy receipt name so retained-source audits continue to verify old runs.
"""

import inspect
from pathlib import Path


async def retained_dashboard_export(runtime, export_format, filters):
    target = await runtime.read_dashboard_context(
        filters if any(value is not None for value in filters.values()) else None
    )
    context = target["context"]
    method = {
        "csv": runtime.analysis_service.export_csv,
        "zip": runtime.analysis_service.export_zip,
        "brief": runtime.analysis_service.export_brief,
    }[export_format]
    content = await method(context["context_id"], locale=runtime.language)
    extension = "md" if export_format == "brief" else export_format
    path = (
        Path(runtime.toolkit.run_dir)
        / "artifacts"
        / (f"retained-{context['context_id'][:12]}-{runtime.language}.{extension}")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    artifact = runtime.toolkit.ledger.register_artifact(
        path, kind="scoped_data_export", context=context, locale=runtime.language
    )
    result = {
        "ok": True,
        "artifact": artifact,
        "context": context,
        "export_locale": runtime.language,
        "export_format": export_format,
        "value_locale": "machine",
    }
    if export_format == "brief":
        source = next(
            (
                entry
                for entry in runtime.toolkit.ledger.records()
                if entry.get("execution_kind") == "canonical_receipt_import"
                and entry.get("analysis_context") == context
            ),
            None,
        )
        if source is None:
            source = runtime.import_dashboard_receipt(target, "dashboard_compare_scope")
        result["source_binding"] = {
            **{
                key: source[key]
                for key in ("execution_id", "code_sha256", "result_sha256", "entry_sha256")
            },
            "context_id": context["context_id"],
        }
    runtime.toolkit.ledger.record(
        "analysis_export",
        inspect.getsource(retained_dashboard_export),
        result,
        execution_kind="bounded_analysis",
        analysis_context=context,
    )
    return {"status": "completed", "scope": context["filters"], "result": result}
