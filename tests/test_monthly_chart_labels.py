"""Canonical monthly labels localize without changing source cells or claims."""

import json

import pytest
from analyst.chart_views import create_chart_view, validate_chart_view
from analyst.evidence import EvidenceLedger
from analyst.runtime import table_selection
from PIL import Image
from test_chart_views import arguments, context


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("x", ["label", "month"])
@pytest.mark.parametrize("kind", ["line", "bar"])
def test_monthly_presentation_keeps_original_cells_and_localizes_the_axis(
    tmp_path, language, x, kind
):
    ledger = EvidenceLedger(tmp_path)
    rows = [
        {"month": "2024-01", "label": "Jan 2024", "event_count": 31},
        {"month": "2024-02", "label": "Feb 2024", "event_count": None},
        {"month": "2024-03", "label": "Mar 2024", "event_count": 39},
    ]
    entry = ledger.record(
        "analysis_query",
        "# Synthetic canonical monthly fixture, not case-study observations",
        {"ok": True, "analysis": {"monthly": rows}, "context": context()},
        analysis_context=context(),
        execution_kind="bounded_analysis",
    )
    original = ledger.path.read_bytes()
    result = create_chart_view(
        ledger,
        arguments(entry, path="$.analysis.monthly", x=x, kind=kind, offset=1, limit=2),
        language=language,
        select_rows=table_selection,
    )
    chart = result["chart"]
    assert chart["axis_labels"]["x"] == ("Tháng" if language == "vi" else "Month")
    assert chart["label_presentation"] == {
        "dimension": "month",
        "labels": [
            {
                "source_path": f"$.analysis.monthly[{index}].month",
                "source_value": rows[index]["month"],
                "text": text,
            }
            for index, text in zip(
                (1, 2),
                ("Tháng 2/2024", "Tháng 3/2024") if language == "vi" else ("Feb 2024", "Mar 2024"),
                strict=True,
            )
        ],
    }
    assert [point["label"] for point in chart["points"]] == [row[x] for row in rows[1:]]
    assert [point["label_path"] for point in chart["points"]] == [
        f"$.analysis.monthly[{index}].{x}" for index in (1, 2)
    ]
    assert [point["value"] for point in chart["points"]] == [None, 39]
    assert [point["value_path"] for point in chart["points"]] == [
        f"$.analysis.monthly[{index}].event_count" for index in (1, 2)
    ]
    with Image.open(ledger.resolve_artifact(result["artifact"]["path"])) as image:
        assert json.loads(image.info["Description"]) == chart
        image.load()
    validate_chart_view(ledger, chart, select_rows=table_selection)
    assert ledger.path.read_bytes() == original


def test_generic_label_and_month_columns_do_not_claim_a_canonical_month_axis(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record(
        "analysis_query",
        "# Generic source labels must remain literal",
        {
            "ok": True,
            "analysis": {"rows": [{"month": "2024-01", "label": "Jan 2024", "event_count": 1}]},
            "context": context(),
        },
        analysis_context=context(),
        execution_kind="bounded_analysis",
    )
    chart = create_chart_view(
        ledger,
        arguments(entry, x="label"),
        language="vi",
        select_rows=table_selection,
    )["chart"]
    assert chart["axis_labels"]["x"] == "label"
    assert "label_presentation" not in chart
    assert chart["points"][0]["label"] == "Jan 2024"
