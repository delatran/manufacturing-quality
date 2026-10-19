"""Brief recommendations preserve unavailable and valid location comparators."""

from __future__ import annotations

import copy

import pytest
from analyst.case_study.analytics import Scope, analysis_report
from analyst.case_study.brief import render_brief
from test_dashboard import Loader, event, make_client, make_snapshot, service


def critical_priority(text):
    return text.split("\n### ", 1)[1].split("\n### ", 1)[0]


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("population", ["filtered_location", "one_location_source"])
def test_brief_download_has_no_target_from_an_absent_location_comparator(
    tmp_path, language, population
):
    rows = [event(1), event(2, severity="Minor")]
    filters = {}
    if population == "filtered_location":
        rows.append(event(3, defect_location="Surface"))
        filters["defect_location"] = "Component"
    subject = service(tmp_path, Loader(make_snapshot(rows)))
    with make_client(subject) as client:
        payload = client.get("/api/dashboard", params=filters).json()
        recommendation = payload["analysis"]["recommendations"][0]
        assert recommendation["kind"] == "critical_location"
        assert recommendation["metrics"]["other_locations_critical_share_pct"] is None
        kpis = payload["analysis"]["kpis"]
        assert (kpis["event_count"], kpis["critical_count"], kpis["known_repair_cost"]) == (
            2,
            1,
            20,
        )
        response = client.get(payload["links"]["brief"], params={"locale": language})
        assert response.status_code == 200
        priority = critical_priority(response.text)
        assert "N/A" not in priority
        if language == "en":
            assert "against" not in priority and "other locations" not in priority
            assert recommendation["success_measure"] in priority
            assert "within two months" not in priority
        else:
            assert "so với" not in priority and "các vị trí khác" not in priority
            assert "chỉ đặt mục tiêu sau khi xác minh đường cơ sở" in priority
            assert "trong hai tháng" not in priority
        after = client.get(f"/api/dashboard/contexts/{payload['context']['context_id']}").json()
        assert after["analysis"] == payload["analysis"]
        assert after["context"] == payload["context"]
        assert after["evidence"] == payload["evidence"]


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("other_share", [0, 50])
def test_available_location_comparator_keeps_evidence_and_target_including_zero(
    language, other_share
):
    snapshot = make_snapshot(
        [
            event(1),
            event(2, severity="Minor"),
            event(3, defect_location="Surface", severity="Minor"),
            event(
                4,
                defect_location="Surface",
                severity="Critical" if other_share else "Minor",
            ),
        ]
    )
    analysis = analysis_report(snapshot, Scope())
    before = copy.deepcopy(analysis)
    recommendation = analysis["recommendations"][0]
    assert recommendation["metrics"]["location"] == "Component"
    assert recommendation["metrics"]["other_locations_critical_share_pct"] == other_share
    priority = critical_priority(
        render_brief({"context": {"context_id": "c" * 64}, "analysis": analysis}, language)
    )
    if language == "en":
        assert f", against {other_share:.2f}% elsewhere;" in priority
        assert (
            f"Provisional target: bring the share down to the other locations' {other_share:.2f}% "
            "within two months of the corrective action." in priority
        )
    else:
        value = f"{other_share:.2f}".replace(".", ",")
        assert f", so với {value}% ở các vị trí khác;" in priority
        assert (
            f"Mục tiêu tạm thời: đưa tỷ trọng về mức {value}% của các vị trí khác "
            "trong hai tháng sau biện pháp khắc phục." in priority
        )
    assert analysis == before
