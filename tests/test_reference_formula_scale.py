"""Source-bound formula scale acceptance without weakening measured units."""

import pytest
from analyst.runtime import AnalystReport, NumericClaim, audit_report
from test_reference_output_unit import denominator_source as denominator_source


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "count",
        "cost",
        "operator",
        "value",
        "scale_unit",
        "unknown",
        "duplicate",
        "trailing",
        "other_receipt",
        "locale",
    ],
)
async def test_reference_formula_scale(denominator_source, language, damage):
    ledger, entry, claims = denominator_source
    scale = NumericClaim(
        id="scale",
        execution_id=entry["execution_id"],
        path="$.metric_metadata.reference_intensity_per_1000.scale",
        value=1000,
        code_sha256=entry["code_sha256"],
    )
    claims["scale"] = scale

    def shown(name):
        value = claims[name].value
        text = f"{value:,.8f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
        if language == "vi":
            text = text.translate(str.maketrans({",": ".", ".": ","}))
        return text + f" [claim:{name}]"

    numerator = (
        "event_count"
        if damage == "count"
        else "known_repair_cost"
        if damage == "cost"
        else "eligible_event_count"
    )
    label = "unit scale" if language == "en" else "hệ số quy đổi"
    text = f"{shown(numerator)} ÷ {shown('reference_output')} × {label} {shown('scale')} = {shown('reference_intensity_per_1000')}."
    if damage == "operator":
        text = text.replace("÷", "+")
    if damage == "value":
        text = text.replace("1,000", "2,000").replace("1.000", "2.000")
    if damage == "scale_unit":
        text = text.replace(" [claim:scale]", " events [claim:scale]")
    if damage == "locale":
        text = text.replace(
            shown("scale"), ("1.000" if language == "en" else "1,000") + " [claim:scale]"
        )
    if damage == "unknown":
        text = text.replace("[claim:scale]", "[claim:unknown]")
    if damage == "duplicate":
        text = text.replace(shown("scale"), shown("scale") + " 1000")
    if damage == "trailing":
        text += " Observed 1000 events [claim:scale]."
    if damage == "other_receipt":
        claims["scale"] = scale.model_copy(update={"execution_id": "another-context-receipt"})
    report = AnalystReport(
        report=text,
        claims=list(claims.values()),
        number_format="vi-VN" if language == "vi" else "en-US",
    )
    result = audit_report(report, ledger)
    assert result["verified"] is (damage is None), result
