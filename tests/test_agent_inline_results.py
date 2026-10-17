"""Agent answers stay in chat while Dashboard exports remain available."""

import asyncio

import pytest
from analyst.config import Settings
from analyst.runtime import AnalystRuntime, ReportDraft
from analyst.tools import AnalystTools
from pydantic import ValidationError


def test_model_output_selects_inline_charts_instead_of_downloadable_results():
    schema = ReportDraft.model_json_schema()
    assert set(schema["properties"]) == {"report", "chart_paths"}
    assert ReportDraft(report="An in-chat answer.").chart_paths == []
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReportDraft.model_validate(
            {"report": "An in-chat answer.", "deliverable_paths": ["artifacts/report.csv"]}
        )


def test_verified_chat_answer_does_not_generate_a_report_bundle(tmp_path):
    async def scenario():
        source = tmp_path / "source.csv"
        source.write_text("group,value\na,2\na,4\nb,6\n", encoding="utf-8")
        tools = AnalystTools(tmp_path / "run", [source])
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        runtime = AnalystRuntime(Settings(mode="mock", memory_enabled=False), tools, emit)
        try:
            result = await runtime.run("Analyze the supplied data.")
            assert result["status"] == "completed", result["qa"]
            assert result["qa"]["verified"]
            assert result["report"] and result["claims"]
            assert result["artifacts"] == result["delivery"]["artifacts"] == []
            assert runtime.operation_capabilities()["result_exports_available"] is False
            generated = {item["name"] for item in tools.ledger.artifacts()}
            assert not generated.intersection(
                {
                    "business_report.html",
                    "verified_report.md",
                    "verified_claims.json",
                    "verification_scope.json",
                    "verified_evidence.zip",
                    "execution_ledger.jsonl",
                }
            )
            assert "distribution.png" in generated
            assert len([event for event in events if event[0] == "report"]) == 1
            assert tools.ledger.verify_claims(result["claims"])["verified"]
        finally:
            await tools.close()

    asyncio.run(scenario())
