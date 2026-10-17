"""Agent output stays in chat while internal evidence and inline charts remain."""

import pytest
from analyst import prompts
from analyst.product_tools import product_schemas


@pytest.mark.parametrize("instructions", [prompts.COMMON, prompts.PRODUCT_LEAD])
def test_model_contract_selects_only_inline_charts_and_in_chat_report(instructions):
    assert "chart_paths" in instructions
    assert "deliverable_paths" not in instructions
    assert "analysis_export" not in instructions
    assert "Dashboard export controls" in instructions
    assert "PNG, JPEG" in instructions and "GIF or SVG" in instructions
    assert "report" in instructions and "Markdown" in instructions
    assert "copy templates, code blocks or file-content substitutes" in instructions


def test_final_qa_accepts_chat_results_when_agent_file_export_is_unavailable():
    assert "File\nexports are unavailable in Agent" in prompts.QA
    assert "never demand an Agent result file or download link" in prompts.QA
    assert "Reject a promised file or CSV copy template" in prompts.QA
    assert "numeric_support" in prompts.QA and "table_support" in prompts.QA
    assert "report_table" in prompts.COMMON
    assert "analysis_sql" in {item["name"] for item in product_schemas()}
    assert "investigation_update" in {item["name"] for item in product_schemas()}
