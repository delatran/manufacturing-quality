"""Structural ordinals, typed scales and value echoes keep distinct purposes."""

from __future__ import annotations

import pytest
from analyst.report_output import adjacent_value_error, typed_unit_scale_text
from analyst.runtime import AnalystReport, audit_report
from test_runtime_claim_grammar import grammar_subject as grammar_subject
from test_runtime_claim_grammar import report


@pytest.mark.parametrize(
    "text,valid",
    [
        ("### Priority 1: Investigate reported events", True),
        ("### Priority 2: Validate the exposure denominator", True),
        ("### Ưu tiên 3: Kiểm tra dữ liệu chi phí", True),
        ("### Priority 1: Observed 999 events", False),
        ("### Priority 999 events: A measured amount", False),
        ("The priority has 1 event.", False),
        ("### Observed 1 event", False),
        ("### Priority 1 USD", False),
        ("1. Investigate the process", True),
    ],
)
def test_structural_ordinal_is_not_a_quantitative_claim(grammar_subject, text, valid):
    ledger, _, _, _ = grammar_subject
    original = AnalystReport(report=text, claims=[])
    audited = audit_report(original, ledger)
    assert audited["verified"] is valid, audited
    assert original.report == text


@pytest.mark.parametrize(
    "text",
    [
        "**1,071 total defect events** 1,071",
        "**508,864.39 local currency** 508,864.39",
        "Known events: **1,049 of 1,071** 1,049 / 1,071",
        "Unknown events: **22** 22",
        "Cường độ: **1,7952 sự kiện** 1,7952",
        "__22 events__22",
        "1,071 total defect events 1,071",
        "1.071 sự kiện lỗi 1.071",
        "508,864.39 local currency 508,864.39",
    ],
)
def test_inserted_value_cannot_echo_an_adjacent_emphasized_value(text):
    original = text
    assert adjacent_value_error(text)
    assert text == original


@pytest.mark.parametrize(
    "text",
    [
        "Count **20** and cost **20**.",
        "**20 events** and **20 local currency**.",
        "January **20 events**; June **20 events**.",
        "**Count** 20 events; **Cost** 20 local currency.",
        "**20 events** | **20 events**",
        "**20 events**. Summary: **20 events**.",
        "**Product ID** 20",
        "**20 events** + 20 events",
        "**20 of 20 events**",
        "**20** out of **20** events",
        "**20 trên 20 sự kiện**",
        "20 trong tổng số 20 sự kiện",
        "**Reference intensity** 1.7952 events per thousand units.",
    ],
)
def test_distinct_labelled_values_conjunctions_and_repeated_summaries_remain_valid(text):
    assert adjacent_value_error(text) is None


@pytest.mark.parametrize("language", ["en", "vi"])
def test_bound_intensity_unit_before_trailing_citation_masks_only_its_scale(
    grammar_subject, language
):
    ledger, entry, _, claims = grammar_subject
    text = (
        "Reference-output intensity: **1.7952 defect events per 1,000 [claim:scale] reference-output units** 1.7952 [claim:intensity]."
        if language == "en"
        else "Cường độ tham chiếu: **1,7952 sự kiện trên 1.000 [claim:scale] đơn vị tham chiếu** 1,7952 [claim:intensity]."
    )
    proposal = report(text, [claims["intensity"], claims["scale"]], language)
    masked = typed_unit_scale_text(
        proposal.report,
        [claim.model_dump() for claim in proposal.claims],
        {entry["execution_id"]: entry},
        proposal.number_format,
    )
    assert "source-defined scale" in masked
    audited = audit_report(proposal, ledger)
    assert not audited["verified"]
    assert any(
        "repeats a value" in error or "repeated numeric value" in error
        for error in audited["errors"]
    )
    assert not any(issue.get("value") == 1000 for issue in audited["numeric_issues"])


@pytest.mark.parametrize("language", ["en", "vi"])
def test_marker_once_inside_emphasis_and_literal_with_claim_remain_valid(grammar_subject, language):
    ledger, _, _, claims = grammar_subject
    text = (
        "Reference-output intensity: **[value:intensity] defect events per 1,000 [claim:scale] reference-output units**."
        if language == "en"
        else "Cường độ tham chiếu: **[value:intensity] sự kiện trên 1.000 [claim:scale] đơn vị tham chiếu**."
    )
    proposal = report(text, [claims["intensity"], claims["scale"]], language)
    assert audit_report(proposal, ledger)["verified"]
    literal = (
        "**2024 [claim:count] events**" if language == "en" else "**2.024 [claim:count] sự kiện**"
    )
    assert audit_report(report(literal, [claims["count"]], language), ledger)["verified"]


@pytest.mark.parametrize("metric", ["count", "cost", "day", "products"])
def test_unit_scale_cannot_attach_to_another_metric_through_a_trailing_number(
    grammar_subject, metric
):
    ledger, _, _, claims = grammar_subject
    proposal = report(
        "Reference output: **per 1,000 [claim:scale] reference-output units** [value:"
        + metric
        + "].",
        [claims[metric], claims["scale"]],
    )
    assert not audit_report(proposal, ledger)["verified"]
    assert any(
        issue.get("value") == 1000 for issue in audit_report(proposal, ledger)["numeric_issues"]
    )
