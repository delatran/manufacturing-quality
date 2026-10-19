"""Render a shareable Markdown quality brief from one backend analysis payload.

The brief reformats numbers that the analysis already contains; it computes no new
metrics. The README generator reuses the same priority wording, so the dashboard,
README and brief describe each priority with identical evidence.
"""

from __future__ import annotations

from typing import Any

FILTER_LABELS = {
    "production_line": ("Line", "Chuyền"),
    "category": ("Category", "Nhóm sản phẩm"),
    "product_id": ("Product", "Sản phẩm"),
    "severity": ("Severity", "Mức độ"),
    "defect_type": ("Defect type", "Loại lỗi"),
    "defect_location": ("Location", "Vị trí"),
    "inspection_method": ("Inspection method", "Phương pháp kiểm tra"),
}


def number(value: int | float | None, language: str = "en", decimals: int = 0) -> str:
    if value is None:
        return "N/A"
    text = format(value, f",.{decimals}f")
    return text.translate({ord(","): ".", ord("."): ","}) if language == "vi" else text


def priority_block(
    item: dict[str, Any], analysis: dict[str, Any], language: str, heading_level: int = 4
) -> list[str]:
    """Render one backend recommendation with its structured evidence."""
    vi = language == "vi"
    metric = item.get("metrics", {})

    def n(value: int | float | None, decimals: int = 0) -> str:
        return number(value, language, decimals)

    def pct(value: float | None) -> str:
        return "N/A" if value is None else n(value, 2) + "%"

    labels = (
        ("Bằng chứng", "Hành động", "Phụ trách", "Thước đo thành công")
        if vi
        else ("Evidence", "Action", "Owner", "Success measure")
    )
    kind = item.get("kind")
    if kind == "critical_location":
        other_share = metric["other_locations_critical_share_pct"]
        comparison = (
            (
                f", so với {pct(other_share)} ở các vị trí khác"
                if vi
                else f", against {pct(other_share)} elsewhere"
            )
            if other_share is not None
            else ""
        )
        title = (
            f"Điều tra lỗi nghiêm trọng (Critical) tại {metric['location']}"
            if vi
            else f"Investigate Critical defects at {metric['location']}"
        )
        evidence = (
            f"{n(metric['critical_count'])}/{n(metric['event_count'])} sự kiện tại {metric['location']} là Critical ({pct(metric['critical_share_pct'])}){comparison}; {metric['location']} chiếm {pct(metric['share_of_all_critical_pct'])} tổng số lỗi Critical."
            if vi
            else f"{n(metric['critical_count'])} of {n(metric['event_count'])} {metric['location']} events are Critical ({pct(metric['critical_share_pct'])}){comparison}; {metric['location']} holds {pct(metric['share_of_all_critical_pct'])} of all Critical events."
        )
        action = (
            f"Lấy {n(metric['critical_count'])} bản ghi Critical, tháo và phân tích lỗi đại diện theo từng dòng sản phẩm, kiểm tra lô linh kiện đầu vào và thông số lắp ráp; thêm bước kiểm khoanh vùng tại khâu kiểm {metric['location']} cho đến khi xác nhận nguyên nhân."
            if vi
            else f"Pull the {n(metric['critical_count'])} Critical records, tear down representative failures per product family and check incoming component lots and assembly settings; add a containment check at {metric['location']} inspection until the cause is confirmed."
        )
        owner = (
            "Kỹ sư Chất lượng (chủ trì) cùng Trưởng nhóm sản xuất của các chuyền liên quan."
            if vi
            else "Quality Engineer (lead) with the Production Team Leaders of the affected lines."
        )
        if other_share is None:
            success = (
                f"Theo dõi số sự kiện Critical và tỷ trọng của chúng tại {metric['location']}, kèm sản lượng thực tế, số đơn vị đã kiểm tra và phạm vi kiểm tra tương đương; chỉ đặt mục tiêu sau khi xác minh đường cơ sở."
                if vi
                else item["success_measure"]
            )
        else:
            success = (
                f"Số và tỷ trọng Critical tại {metric['location']} theo tháng. Mục tiêu tạm thời: đưa tỷ trọng về mức {pct(other_share)} của các vị trí khác trong hai tháng sau biện pháp khắc phục."
                if vi
                else f"Monthly Critical count and share at {metric['location']}. Provisional target: bring the share down to the other locations' {pct(other_share)} within two months of the corrective action."
            )
    elif kind == "category_intensity":
        compared = metric.get("lines_compared")
        lines = ""
        if compared:
            low = n(metric["min_intensity_ratio_to_rest_of_line"], 2)
            high = n(metric["max_intensity_ratio_to_rest_of_line"], 2)
            above = n(metric["lines_above_rest_of_line"])
            lines = (
                f"; cao hơn phần còn lại của chuyền ở {above}/{n(compared)} chuyền (gấp {low}–{high} lần)"
                if vi
                else f"; higher than the rest of the line on {above} of {n(compared)} lines ({low}–{high}×)"
            )
        leaders = []
        for row in analysis["products"]["intensity_all"]:
            if row.get("category") != metric["category"]:
                break
            leaders.append(str(row["product_id"]))
        title = (
            f"Điều tra sản phẩm {metric['category']} có cường độ lỗi cao"
            if vi
            else f"Investigate high-intensity {metric['category']} products"
        )
        if vi:
            evidence = (
                f"{n(metric['eligible_event_count'])} sự kiện trên {n(metric['reference_output'])} đơn vị tham chiếu ({n(metric['reference_intensity_per_1000'], 3)} trên 1.000) so với {n(metric['rest_of_scope_reference_intensity_per_1000'], 3)} của các sản phẩm khác{lines}"
                + (f"; các sản phẩm {', '.join(leaders)} dẫn đầu cường độ lỗi." if leaders else ".")
            )
        else:
            evidence = (
                f"{n(metric['eligible_event_count'])} events over {n(metric['reference_output'])} reference units ({n(metric['reference_intensity_per_1000'], 3)} per 1,000) against {n(metric['rest_of_scope_reference_intensity_per_1000'], 3)} for all other products{lines}"
                + (f"; products {', '.join(leaders)} lead product intensity." if leaders else ".")
            )
        action = (
            f"So sánh thiết kế, vật liệu và các bước công đoạn của {metric['category']} (kể cả việc chuyển đổi sản xuất ở các sản phẩm sản lượng thấp này) với các nhóm khác; bắt đầu từ các sản phẩm dẫn đầu và thử biện pháp trên một chuyền trước khi nhân rộng."
            if vi
            else f"Compare {metric['category']} design, materials and process steps (including changeovers on these low-volume products) with the other categories; start with the leading products and pilot a fix on one line before rolling it out."
        )
        owner = (
            "Kỹ thuật quy trình/sản phẩm (chủ trì) cùng Kỹ sư Chất lượng và trưởng nhóm của các chuyền sản xuất nhóm này."
            if vi
            else "Process/Product Engineering (lead) with the Quality Engineer and the team leaders of every line that makes the category."
        )
        success = (
            f"Cường độ lỗi của {metric['category']} trên từng chuyền và tỷ lệ so với phần còn lại của chuyền. Mục tiêu tạm thời: giảm một nửa khoảng chênh {n(metric['intensity_gap_per_1000'], 3)} trên 1.000 trong một quý."
            if vi
            else f"{metric['category']} events per 1,000 reference units on each line and its ratio to the rest of the line. Provisional target: halve the {n(metric['intensity_gap_per_1000'], 3)}-per-1,000 gap within one quarter."
        )
    elif kind == "line_repair_cost":
        family = next(
            (
                (control["category"], row)
                for control in analysis.get("shared_category_controls", [])
                for row in control["lines"]
                if row["production_line"] == metric["production_line"]
                and (row["cost_ratio_to_same_category_other_lines"] or 0) > 1
            ),
            None,
        )
        within = ""
        if family:
            inside = n(family[1]["average_known_repair_cost"], 2)
            elsewhere = n(family[1]["same_category_other_lines_average_known_repair_cost"], 2)
            within = (
                f"; chênh lệch vẫn còn trong {family[0]} ({inside} so với {elsewhere})"
                if vi
                else f"; the gap persists within {family[0]} ({inside} vs {elsewhere})"
            )
        title = (
            f"Điều tra chi phí sửa chữa mỗi sự kiện tại {metric['production_line']}"
            if vi
            else f"Investigate repair cost per event on {metric['production_line']}"
        )
        evidence = (
            f"{n(metric['average_known_repair_cost'], 2)} mỗi sự kiện ghi nhận có chi phí hợp lệ so với {n(metric['other_lines_average_known_repair_cost'], 2)} ở các chuyền khác (gấp {n(metric['ratio_to_other_lines'], 2)} lần); {pct(metric['known_cost_share_pct'])} chi phí đã biết của các sự kiện đã liên kết đến từ {pct(metric['known_cost_event_share_pct'])} số sự kiện đã liên kết có chi phí hợp lệ{within}."
            if vi
            else f"{n(metric['average_known_repair_cost'], 2)} per logged event with known cost against {n(metric['other_lines_average_known_repair_cost'], 2)} on the other lines ({n(metric['ratio_to_other_lines'], 2)}×); {pct(metric['known_cost_share_pct'])} of linked known cost from {pct(metric['known_cost_event_share_pct'])} of linked events with known cost{within}."
        )
        action = (
            f"Cùng Bảo trì tách chi phí sửa của {metric['production_line']} theo nhân công, vật tư, làm lại và dịch vụ ngoài; so sánh cách sửa các sản phẩm tương đương trong cùng nhóm ở chuyền khác, kiểm tra chênh lệch cơ cấu sản phẩm còn lại; xác nhận với Tài chính rằng các chuyền ghi nhận chi phí theo cùng quy tắc."
            if vi
            else f"Break {metric['production_line']} repair cost into labour, parts, rework and external service with Maintenance; compare repair routes for comparable products in the same category on other lines and check remaining product-mix differences; confirm with Finance that every line records cost the same way."
        )
        owner = (
            f"Trưởng nhóm {metric['production_line']} (chủ trì) cùng Trưởng bộ phận Bảo trì và Tài chính."
            if vi
            else f"{metric['production_line']} Team Leader (lead) with the Maintenance Lead and Finance."
        )
        success = (
            f"Chi phí sửa trung bình mỗi sự kiện của {metric['production_line']} so với các chuyền khác, kèm độ phủ chi phí. Mục tiêu tạm thời: thu hẹp một nửa khoảng chênh {n(metric['cost_gap_per_event'], 2)} mỗi sự kiện trong một quý."
            if vi
            else f"{metric['production_line']} average known repair cost per event against other lines, with cost coverage. Provisional target: close half of the {n(metric['cost_gap_per_event'], 2)}-per-event gap within one quarter."
        )
    elif kind == "line_intensity" and vi:
        title = f"Xác minh gánh nặng lỗi tại {metric['production_line']}"
        evidence = f"{n(metric['event_count'])} sự kiện; {n(metric['reference_intensity_per_1000'], 3)} trên 1.000 đơn vị sản lượng tham chiếu."
        action = "So sánh cơ cấu sản phẩm và loại lỗi, xác nhận sản lượng thực tế và phạm vi kiểm tra, rồi cùng nhóm chuyền điều tra lỗi lặp lại."
        owner = f"Trưởng nhóm {metric['production_line']} cùng Kế hoạch sản xuất."
        success = (
            "Tính lại số sự kiện trên sản lượng thực tế và số lỗi Critical ở quy mô tương đương."
        )
    elif kind == "defect_type_cost" and vi:
        title = f"Rà soát chi phí sửa lỗi {metric['defect_type']} và việc ghi nhận chi phí"
        evidence = f"{metric['defect_type']} chiếm {n(metric['known_repair_cost'], 2)} chi phí đã biết ({pct(metric['known_cost_share_pct'])}); {n(metric['cost_unknown_count'])} sự kiện chưa có chi phí."
        action = (
            "Cùng Bảo trì rà soát các sửa chữa tốn kém và đối soát chi phí thiếu với Tài chính."
        )
        owner = "Trưởng bộ phận Bảo trì, Kỹ sư Chất lượng và Tài chính."
        success = "Theo dõi đồng thời chi phí đã biết, chi phí trung bình mỗi sự kiện có chi phí và độ phủ chi phí."
    else:
        title, evidence, action, owner, success = (
            item["title"],
            item["evidence"],
            item["action"],
            item["owner"],
            item["success_measure"],
        )
    heading = (
        "#" * heading_level + f" {'Ưu tiên' if vi else 'Priority'} {item['priority']}: {title}"
    )
    return [
        heading,
        "",
        f"- **{labels[0]}:** {evidence}",
        f"- **{labels[1]}:** {action}",
        f"- **{labels[2]}:** {owner}",
        f"- **{labels[3]}:** {success}",
    ]


def render_brief(payload: dict[str, Any], language: str = "en") -> str:
    """A one-page brief for sharing a filtered view with another department."""
    vi = language == "vi"
    analysis = payload["analysis"]
    context = payload["context"]
    k = analysis["kpis"]
    scope = analysis["scope"]
    trend = analysis["trend"]
    halves = analysis.get("period_halves", {"enabled": False})
    screen = analysis["anomalies"]
    coverage = analysis["calendar_coverage"]

    def n(value: int | float | None, decimals: int = 0) -> str:
        return number(value, language, decimals)

    def pct(value: float | None) -> str:
        return "N/A" if value is None else n(value, 2) + "%"

    def signed(value: float | None) -> str:
        return "N/A" if value is None else ("+" if value > 0 else "") + pct(value)

    filters = [
        f"{FILTER_LABELS[field][1 if vi else 0]}: {scope[field]}"
        for field in FILTER_LABELS
        if scope.get(field)
    ]
    scenario = scope.get("scenario") or ("canonical" if not vi else "chuẩn (canonical)")
    title = ", ".join(filters) if filters else ("toàn nhà máy" if vi else "whole plant")
    period = f"{scope['start_month']} → {scope['end_month']}"
    if trend["absolute_change"] > 0:
        signal = "tín hiệu tiêu cực" if vi else "an adverse signal"
    elif trend["absolute_change"] < 0:
        signal = "tín hiệu tích cực" if vi else "a favourable signal"
    else:
        signal = "không đổi" if vi else "no change"
    lines = [
        f"# {'Bản tin chất lượng' if vi else 'Quality brief'}: {title}",
        "",
        (
            f"Kỳ {period} · scenario {scenario} · context `{context['context_id'][:16]}` · nguồn lấy lúc {analysis['metadata']['fetched_at']}. Backend tạo bản tin từ cùng kết quả phân tích với dashboard; tiền tệ là local currency (chưa xác định loại tiền)."
            if vi
            else f"Period {period} · scenario {scenario} · context `{context['context_id'][:16]}` · source fetched {analysis['metadata']['fetched_at']}. Generated by the backend from the same analysis as the dashboard; amounts are in local currency (unspecified)."
        ),
        "",
        "## " + ("Tóm tắt" if vi else "Headline"),
        "",
        (
            f"- **{n(k['event_count'])} sự kiện lỗi được ghi nhận**, trong đó {n(k['critical_count'])} Critical ({pct(k['critical_share_pct'])})."
            if vi
            else f"- **{n(k['event_count'])} logged defect events**, {n(k['critical_count'])} of them Critical ({pct(k['critical_share_pct'])})."
        ),
    ]
    if trend["first_month"] != trend["last_month"]:
        lines.append(
            f"- **Xu hướng:** {n(trend['first_count'])} ({trend['first_month']}) → {n(trend['last_count'])} ({trend['last_month']}), {signed(trend['change_pct'])}; {signal}."
            if vi
            else f"- **Trend:** {n(trend['first_count'])} ({trend['first_month']}) → {n(trend['last_count'])} ({trend['last_month']}), {signed(trend['change_pct'])}; {signal}."
        )
    if halves.get("enabled"):
        first, second = halves["first"], halves["second"]
        lines.append(
            f"- **Hai nửa giai đoạn:** {n(first['event_count'])} → {n(second['event_count'])} sự kiện ({signed(halves['event_count_change_pct'])}); Critical {signed(halves['critical_count_change_pct'])}; chi phí ghi nhận {signed(halves['known_repair_cost_change_pct'])}."
            if vi
            else f"- **First vs second half:** {n(first['event_count'])} → {n(second['event_count'])} events ({signed(halves['event_count_change_pct'])}); Critical {signed(halves['critical_count_change_pct'])}; observed cost {signed(halves['known_repair_cost_change_pct'])}."
        )
    lines += [
        (
            f"- **Chi phí sửa chữa đã biết:** {n(k['known_repair_cost'], 2)}, độ phủ chi phí {pct(k['cost_coverage_pct'])}; {n(k['cost_unknown_count'])} sự kiện chưa có chi phí hợp lệ."
            if vi
            else f"- **Observed repair cost:** {n(k['known_repair_cost'], 2)} with {pct(k['cost_coverage_pct'])} cost coverage; {n(k['cost_unknown_count'])} events lack a valid cost."
        ),
        (
            f"- **Cường độ tham chiếu:** {n(k['reference_intensity_per_1000'], 3)} sự kiện trên 1.000 đơn vị sản lượng tham chiếu (monthly_output cố định, chưa phải sản lượng thực)."
            if vi
            else f"- **Reference intensity:** {n(k['reference_intensity_per_1000'], 3)} events per 1,000 reference units (static monthly_output, not actual production)."
        ),
        "",
        "## " + ("Ưu tiên" if vi else "Priorities"),
        "",
    ]
    if analysis["recommendations"]:
        for item in analysis["recommendations"]:
            lines += priority_block(item, analysis, language, heading_level=3) + [""]
    else:
        lines += [
            "Không có sự kiện trong phạm vi này." if vi else "No events in this scope.",
            "",
        ]
    lines += ["## " + ("Ngày cần kiểm tra" if vi else "Days to check"), ""]
    if not screen["enabled"]:
        lines.append(
            "Sàng lọc theo ngày chưa dùng được vì tháng 1 không có sự kiện trong phạm vi này."
            if vi
            else "The daily screen is unavailable because January has no events in this scope."
        )
    elif screen["candidates"]:
        days = ", ".join(f"{row['date']} ({n(row['count'])})" for row in screen["candidates"])
        lines.append(
            f"{n(len(screen['candidates']))} ngày vượt ngưỡng {n(screen['upper_reference_limit'], 2)} sự kiện/ngày tính từ tháng 1: {days}."
            if vi
            else f"{n(len(screen['candidates']))} days exceeded the January-based limit of {n(screen['upper_reference_limit'], 2)} events a day: {days}."
        )
    else:
        lines.append(
            f"Không ngày nào vượt ngưỡng {n(screen['upper_reference_limit'], 2)} sự kiện/ngày tính từ tháng 1."
            if vi
            else f"No day exceeded the January-based limit of {n(screen['upper_reference_limit'], 2)} events a day."
        )
    lines += [
        "",
        "## " + ("Lưu ý dữ liệu" if vi else "Data caveats"),
        "",
        (
            f"- {n(k['unmatched_event_count'])} sự kiện lỗi chưa khớp với danh mục sản phẩm chuẩn; các sự kiện này vẫn nằm trong tổng số lỗi nhưng không tính vào cường độ tham chiếu."
            if vi
            else f"- {n(k['unmatched_event_count'])} events lack a match in the product master; they remain in totals but are excluded from reference intensity."
        ),
        (
            f"- Không tháng nào có sự kiện sau ngày {n(coverage['max_observed_day_of_month'])}; {n(coverage['days_without_events'])}/{n(coverage['calendar_days'])} ngày không có sự kiện. Mức đầy đủ của việc ghi nhận chưa được xác minh."
            if vi and coverage.get("max_observed_day_of_month")
            else f"- No month has events after day {n(coverage['max_observed_day_of_month'])}; {n(coverage['days_without_events'])}/{n(coverage['calendar_days'])} days have none. Reporting completeness is unverified."
            if coverage.get("max_observed_day_of_month")
            else (
                "- Phạm vi này không có ngày nào có sự kiện."
                if vi
                else "- No dated events in this scope."
            )
        ),
        (
            "- Số đếm là sự kiện lỗi được ghi nhận, không phải số sản phẩm hỏng riêng biệt; vai trò phụ trách mang tính đề xuất và bản tin này không thay thế quy trình giao việc chính thức."
            if vi
            else "- Counts represent logged defect events, not distinct defective units. Proposed owners are recommended roles; this brief does not assign tasks or replace plant workflow."
        ),
        "",
    ]
    return "\n".join(lines)
