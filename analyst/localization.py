"""Display-only language support; analytical values and receipts stay unchanged."""

from __future__ import annotations

import re

LANGUAGES = ("en", "vi")
REPORT_NUMBER_FORMAT = "en-US"
VI_NUMBER_NOTE = "Định dạng số: dấu chấm phân tách hàng nghìn, dấu phẩy phân tách phần thập phân."


def report_number_format(language="en"):
    return "vi-VN" if normalize_language(language) == "vi" else "en-US"


def display_number(text, language="en"):
    return text.translate(str.maketrans({",": ".", ".": ","})) if language == "vi" else text


def normalize_language(value="en"):
    if not isinstance(value, str) or value not in LANGUAGES:
        raise ValueError("language must be 'en' or 'vi'")
    return value


def request_language(body):
    if "language" in body and "locale" in body and body["language"] != body["locale"]:
        raise ValueError("language and locale must agree")
    return normalize_language(body.get("language", body.get("locale", "en")))


PHRASES = {
    "Analysis completed; queued instructions are retained after Stop": "Đã hoàn tất phân tích; chỉ dẫn đang chờ được giữ lại sau lệnh Dừng",
    "Analysis cancelled; saved evidence is retained": "Đã hủy phân tích; bằng chứng đã lưu được giữ lại",
    "The app restarted before this run finished. Send a prompt to continue; saved evidence is retained.": "Ứng dụng khởi động lại trước khi lượt chạy hoàn tất. Hãy gửi yêu cầu để tiếp tục; bằng chứng đã lưu được giữ lại.",
    "Lead Analyst is preparing the run": "Trưởng nhóm phân tích đang chuẩn bị lượt chạy",
    "Applying your updated instruction": "Đang áp dụng chỉ dẫn bổ sung của bạn",
    "Analysis completed and evidence checked": "Đã hoàn tất phân tích và kiểm tra bằng chứng",
    "Some claims remain unverified; inspect the QA result": "Một số số liệu chưa được kiểm chứng; hãy xem kết quả kiểm tra",
    "Run stopped at its configured cost limit; pending instructions are retained": "Lượt chạy đã dừng ở giới hạn chi phí; chỉ dẫn đang chờ được giữ lại",
    "The app is stopping; saved evidence is retained": "Ứng dụng đang dừng; bằng chứng đã lưu được giữ lại",
    "Run stopped. Correct the reported issue and send another prompt.": "Lượt chạy đã dừng. Hãy xử lý vấn đề được báo rồi gửi yêu cầu mới.",
    "Your new dashboard scope is queued for a separate run after the current analysis.": "Phạm vi Tổng quan mới được xếp cho một lượt riêng sau phân tích hiện tại.",
    "Your new response language is queued for a separate run after the current analysis.": "Ngôn ngữ trả lời mới được xếp cho một lượt riêng sau phân tích hiện tại.",
    "Your work-item request is queued for a separate run after the current analysis.": "Yêu cầu chuẩn bị công việc được xếp cho một lượt riêng sau phân tích hiện tại.",
    "Choose a dashboard view before asking the agent to prepare a work item": "Chọn phạm vi dữ liệu trước khi nhờ Agent chuẩn bị công việc.",
    "Your instruction is queued for the next safe agent boundary.": "Chỉ dẫn của bạn đang chờ được áp dụng ở bước xử lý phù hợp tiếp theo.",
    "Inspect supplied data and definitions": "Kiểm tra dữ liệu và định nghĩa được cung cấp",
    "Read canonical scope and verify KPI evidence": "Đọc phạm vi chuẩn và kiểm tra bằng chứng KPI",
    "Execute case analysis and sensitivity checks": "Thực hiện phân tích tình huống và kiểm tra độ nhạy",
    "Execute data profiling and summaries": "Khảo sát cấu trúc dữ liệu và tạo thống kê mô tả",
    "Verify report evidence": "Kiểm chứng bằng chứng của báo cáo",
    "Offline simulation using the exact canonical Dashboard evidence.": "Mô phỏng mô hình, sử dụng đúng bằng chứng chuẩn của Tổng quan.",
    "The frozen Dashboard context could not be verified. No alternative snapshot or unfiltered analysis was used.": "Không thể kiểm chứng ngữ cảnh Tổng quan đã chốt. Hệ thống không thay bằng bản dữ liệu khác hoặc phân tích bỏ bộ lọc.",
    "Applied Dashboard scope": "Phạm vi Tổng quan đang áp dụng",
    "This report reuses the frozen Dashboard snapshot and applied filters. It does not refresh data or change the Dashboard.": "Báo cáo dùng lại bản dữ liệu đã chốt và bộ lọc đang áp dụng của Tổng quan, không làm mới dữ liệu hoặc thay đổi Tổng quan.",
    "Scope metadata: ": "Thông tin phạm vi: ",
    "Canonical KPIs": "KPI theo định nghĩa chuẩn",
    "Logged defect events": "Sự kiện lỗi đã ghi nhận",
    "Critical events": "lỗi nghiêm trọng (Critical)",
    "Critical share in percent": "Tỷ trọng sự kiện nghiêm trọng theo phần trăm",
    "Sum of observed repair costs in unspecified local currency": "Tổng chi phí sửa chữa đã ghi nhận theo đơn vị tiền tệ của nguồn, chưa xác định loại tiền",
    "Observed cost coverage in percent": "Tỷ lệ sự kiện có chi phí hợp lệ theo phần trăm",
    "Events without observed valid repair cost": "Sự kiện chưa có chi phí sửa chữa hợp lệ",
    "Linked logged events per thousand reference units": "Sự kiện lỗi liên kết được với danh mục trên mỗi nghìn đơn vị tham chiếu",
    "Counts are logged events, not distinct defective units. Reference intensity uses the eligible master cohort and selected full months; event filters change the numerator only. Static reference output is not actual production or a population defect rate.": "Số đếm là sự kiện lỗi đã ghi nhận, không phải số sản phẩm lỗi riêng biệt. Cường độ tham chiếu dùng toàn bộ nhóm sản phẩm đủ điều kiện trong danh mục và các tháng trọn vẹn đã chọn; bộ lọc sự kiện chỉ thay đổi tử số. Sản lượng tham chiếu cố định không phải sản lượng thực tế hay xác suất sản phẩm lỗi.",
    "Unknown costs are excluded from the observed sum. Missing exposure and reporting coverage prevent causal conclusions.": "Chi phí chưa xác định không được đưa vào tổng chi phí đã ghi nhận. Thiếu sản lượng, số lượng kiểm tra và mức độ đầy đủ của việc ghi nhận nên chưa thể kết luận nhân quả.",
    "Trend in the applied scope": "Xu hướng trong phạm vi đang áp dụng",
    "Only one reporting month is selected; there is no between-month trend comparison.": "Chỉ chọn một tháng báo cáo nên không có so sánh xu hướng giữa các tháng.",
    "Opening and closing logged counts are ": "Số sự kiện đã ghi nhận ở đầu và cuối kỳ lần lượt là ",
    ", respectively.": ".",
    "The canonical count change in percent is ": "Mức thay đổi số sự kiện theo phần trăm, tính theo định nghĩa chuẩn, là ",
    "A percentage change is unavailable because the opening count is zero.": "Không tính được phần trăm thay đổi vì số sự kiện đầu kỳ bằng không.",
    "| Reporting month | Logged events | Critical events | Observed repair cost |": "| Tháng báo cáo | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Chi phí sửa chữa đã ghi nhận |",
    "Location and severity in the applied scope": "Vị trí và mức độ lỗi trong phạm vi đang áp dụng",
    "| Location | Logged events | Critical events | Critical share in percent |": "| Vị trí | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Tỷ trọng nghiêm trọng theo phần trăm |",
    "Within this applied scope, ": "Trong phạm vi đang áp dụng, ",
    " has the largest observed Critical count: ": " có số sự kiện nghiêm trọng đã ghi nhận cao nhất: ",
    "Review representative failures and validate actual production, inspection exposure and reporting completeness before choosing corrective actions.": "Mổ xẻ mẫu lỗi thực tế (failure teardown) và xác minh sản lượng, số lượng kiểm tra cùng mức độ đầy đủ của việc ghi nhận trước khi chọn hành động khắc phục.",
    "No logged events match this applied scope; location priorities cannot be inferred.": "Không có sự kiện lỗi đã ghi nhận phù hợp với phạm vi này; chưa thể suy ra vị trí cần ưu tiên.",
    "Any further SQL or Python work is exploratory and must identify its own scope and denominator. It cannot silently redefine the canonical Dashboard KPIs.": "Các phép tính SQL hoặc Python bổ sung mang tính thăm dò và phải nêu rõ phạm vi cùng mẫu số. Chúng không được âm thầm thay đổi định nghĩa KPI chuẩn của Tổng quan.",
    "A comparison may use another explicitly named scope from the same frozen snapshot without changing Dashboard filters.": "Có thể so sánh với một phạm vi khác được nêu rõ trên cùng bản dữ liệu đã chốt mà không đổi bộ lọc của Tổng quan.",
    "This fixture validates shared evidence and scope binding. Open-ended interpretation requires the configured live model.": "Mô phỏng này kiểm tra việc dùng chung bằng chứng và ràng buộc phạm vi. Diễn giải yêu cầu mở cần mô hình trực tuyến đã cấu hình.",
    "The latest source refresh failed. This report uses the retained frozen snapshot with its original provenance.": "Lần làm mới nguồn gần nhất thất bại. Báo cáo dùng bản dữ liệu đã chốt được giữ lại, với nguồn gốc ban đầu.",
    "The bound source status is stale. This report uses the retained frozen snapshot with its original provenance.": "Trạng thái nguồn đã chốt là chưa cập nhật. Báo cáo dùng bản dữ liệu được giữ lại, với nguồn gốc ban đầu.",
    "Fetch time does not establish event freshness.": "Thời điểm lấy dữ liệu không chứng minh các sự kiện là dữ liệu mới.",
    "Offline simulation with real case-study execution.": "Mô phỏng mô hình với công cụ phân tích tình huống chạy thật.",
    "Offline simulation with real local tools.": "Mô phỏng mô hình với công cụ cục bộ chạy thật.",
    "Observed dataset profile:": "Kết quả khảo sát dữ liệu:",
    "An execution failed. Its failure is retained in the evidence ledger.": "Một lần thực thi thất bại. Thông tin lỗi được giữ trong sổ bằng chứng.",
    "This fixture validates profiling, persistent Python, SQL, exported files and evidence plumbing.": "Mô phỏng này kiểm tra khảo sát dữ liệu, trạng thái Python liên tục, SQL, tệp xuất và đường dẫn bằng chứng.",
    "Open-ended prompt interpretation and analyst judgement require the configured live model.": "Diễn giải yêu cầu mở và nhận định phân tích cần mô hình trực tuyến đã cấu hình.",
    "No population rate or causal claim is inferred from this descriptive fixture.": "Mô phỏng mô tả này không suy ra tỷ lệ của toàn bộ quần thể hoặc kết luận nhân quả.",
    "Analysis could not be verified. Inspect the execution trace and unresolved errors.": "Chưa thể kiểm chứng phân tích. Hãy xem vết thực thi và các lỗi chưa xử lý được.",
    "Case analysis executed; report not available.": "Công cụ đã chạy phân tích tình huống nhưng chưa có báo cáo.",
    "column count": "số cột",
    "duplicate row count": "số dòng trùng",
    "row count": "số dòng",
    "missing cells": "số ô thiếu dữ liệu",
    "production line: ": "dây chuyền sản xuất: ",
    "start month: ": "tháng bắt đầu: ",
    "end month: ": "tháng kết thúc: ",
    "category: ": "nhóm sản phẩm: ",
    "product id: ": "mã sản phẩm: ",
    "severity: ": "mức độ lỗi: ",
    "defect type: ": "loại lỗi: ",
    "defect location: ": "vị trí lỗi: ",
    "inspection method: ": "phương pháp kiểm tra: ",
    "unavailable": "chưa có dữ liệu",
    " and ": " và ",
    "Surface": "Bề mặt",
    "Component": "Linh kiện",
    "Internal": "Bên trong",
    "Functional": "Chức năng",
    "Structural": "Kết cấu",
    "Cosmetic": "Ngoại quan",
    "Critical": "Nghiêm trọng",
    "Major": "Nặng",
    "Moderate": "Trung bình",
    "Minor": "Nhẹ",
    "Unknown": "Chưa xác định",
    "Visual Inspection": "Kiểm tra trực quan",
    "Automated Testing": "Kiểm tra tự động",
    "Manual Testing": "Kiểm tra thủ công",
    "Category ": "Nhóm ",
    "Product ": "Sản phẩm ",
    "numeric token ": "giá trị số ",
    " lacks matching cited evidence": " không có bằng chứng trích dẫn khớp",
    "unknown claim ": "tham chiếu chưa xác định ",
    "missing numeric evidence": "thiếu bằng chứng số liệu",
    "value differs from evidence": "giá trị khác với bằng chứng",
    "code hash mismatch": "mã băm mã thực thi không khớp",
    "The frozen Dashboard context was not successfully verified and imported": "Chưa kiểm chứng và tiếp nhận thành công ngữ cảnh Tổng quan đã chốt",
    "No computational evidence was produced for this analysis": "Phân tích này chưa tạo ra bằng chứng tính toán",
    "The report does not use the selected Vietnamese response language": "Báo cáo chưa dùng ngôn ngữ trả lời tiếng Việt đã chọn",
    "Case-study independent verification did not pass": "Kiểm chứng độc lập của phân tích tình huống chưa đạt",
    "Report export failed: ": "Xuất báo cáo thất bại: ",
    "narrative verification failed: ": "kiểm chứng nội dung báo cáo thất bại: ",
}

PHRASES.update(
    {
        "Manufacturing quality analysis": "Phân tích chất lượng sản xuất",
        "Historical review for January through June of year twenty twenty-four. The findings identify where logged defect burden and observed repair costs warrant investigation, with data limitations carried into each decision.": "Rà soát dữ liệu lịch sử giai đoạn tháng 1 – 6/2024. Các phát hiện chỉ ra những nhóm lỗi và chi phí sửa chữa cần điều tra, đồng thời nêu rõ giới hạn dữ liệu khi ra quyết định.",
        "The period contains ": "Trong kỳ có ",
        "Source rows received": "Dòng dữ liệu nguồn đã nhận",
        "Duplicate copies removed": "Bản sao trùng đã loại bỏ",
        "Conflicting or invalid rows set aside": "Dòng mâu thuẫn hoặc không hợp lệ đã cách ly",
        "Clean events retained": "Sự kiện sạch được giữ lại",
        "Events outside the study period": "Sự kiện ngoài kỳ nghiên cứu",
        "Events included in the analysis": "Sự kiện được đưa vào phân tích",
        "All source rows accounted for": "Đã đối soát toàn bộ dòng dữ liệu nguồn",
        "The quality workbook records the issues found, handling decisions, assumptions and their effect on results. Issue counts overlap and must not be added to infer removals.": "Workbook chất lượng dữ liệu ghi lại các vấn đề đã phát hiện, quyết định xử lý, giả định và ảnh hưởng đến kết quả. Số lượng các vấn đề có thể trùng nhau; không cộng chúng để suy ra số dòng bị loại.",
        "Monthly trend": "Xu hướng theo tháng",
        "| Month | Events | Critical | Known repair cost |": "| Tháng | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Chi phí sửa chữa đã ghi nhận |",
        "Operational patterns": "Các mẫu hình cần lưu ý trong vận hành",
        "Only slash dates with an ambiguous month/day order are changed. The alternative interpretation and withholding scenarios change the in-scope evidence population, so lower totals are not savings or manufacturing improvement. These scenarios are not confidence intervals.": "Chỉ những ngày viết bằng dấu gạch chéo chưa rõ thứ tự tháng/ngày mới được thay đổi. Kịch bản diễn giải theo thứ tự còn lại và kịch bản tạm loại các dòng này làm thay đổi tập dữ liệu trong phạm vi phân tích, nên tổng số thấp hơn không thể hiện tiết kiệm hoặc cải thiện sản xuất. Các kịch bản này không phải khoảng tin cậy.",
        "Adopted month/day convention": "Quy ước tháng/ngày đang sử dụng",
        "Ambiguous dates read as day/month": "Diễn giải ngày chưa rõ theo ngày/tháng",
        "Ambiguous source dates withheld": "Tạm loại ngày nguồn chưa rõ",
        "Priorities, owners and success measures": "Ưu tiên, người phụ trách và thước đo kết quả",
        "Track known repair cost, mean observed cost per event with known cost, and cost coverage together; compare verified repair costs at comparable exposure before setting a savings target.": "Theo dõi đồng thời tổng chi phí sửa chữa đã ghi nhận, chi phí trung bình trên các sự kiện có chi phí hợp lệ và tỷ lệ sự kiện có chi phí hợp lệ; so sánh chi phí được xác minh ở quy mô tương đương trước khi đặt mục tiêu tiết kiệm.",
        "Evidence needed to verify improvement": "Bằng chứng cần có để xác minh cải thiện",
        "Use the local investigation records to document proposed owners, acknowledgements, actions and follow-up reviews. These records do not confirm that another person received an assignment or that a corrective action improved plant performance.": "Dùng hồ sơ điều tra tại chỗ để ghi lại người phụ trách được đề xuất, xác nhận tiếp nhận, hành động và các lần rà soát tiếp theo. Các hồ sơ này không xác nhận rằng người khác đã nhận được việc giao hoặc biện pháp khắc phục đã cải thiện kết quả của nhà máy.",
        "To verify improvement, collect actual produced and inspected units, operating calendars and operator feedback over comparable periods, and complete the missing cost records. Establish a dependable feed of operating records and clear reporting expectations, validate alert usefulness, and connect shared assignments and reviews to the factory operating workflow. Keep human review before operational decisions.": "Để xác minh cải thiện, cần thu thập số lượng sản xuất và kiểm tra thực tế, lịch vận hành và phản hồi từ nhân viên vận hành trong các kỳ có thể so sánh, đồng thời bổ sung bản ghi chi phí còn thiếu. Cần có nguồn cấp dữ liệu vận hành ổn định, yêu cầu ghi nhận rõ ràng, kiểm chứng tính hữu ích của cảnh báo và kết nối việc giao nhiệm vụ, rà soát chung với quy trình vận hành nhà máy. Con người cần rà soát trước khi quyết định vận hành.",
        "The current findings support targeted investigation. They do not establish current plant conditions, confirmed causes or measured savings.": "Các phát hiện hiện tại hỗ trợ xác định trọng tâm điều tra. Chúng chưa xác lập tình trạng hiện tại của nhà máy, nguyên nhân đã được xác nhận hoặc khoản tiết kiệm đã đo lường.",
        "The brief requires a web app dashboard and analytical answers, and contains no numerical answer key. Verified historical answers come from HTTP evidence and an independently reviewed oracle. The current owner requests Dashboard and Agent tabs, with Dashboard as the default, sharing datasets, KPI definitions, analytical filters and execution evidence.": "Đề bài yêu cầu ứng dụng bảng điều khiển và câu trả lời phân tích, không cung cấp đáp án số. Các kết quả lịch sử được kiểm chứng từ dữ liệu lấy qua HTTP và bộ đáp án đối chiếu được xây dựng độc lập. Theo yêu cầu hiện tại, ứng dụng có tab Tổng quan mặc định và tab Trợ lý AI, dùng chung dữ liệu, định nghĩa KPI, bộ lọc phân tích và bằng chứng thực thi.",
        "Scope and data quality": "Phạm vi và chất lượng dữ liệu",
        "January through June of year twenty twenty-four contains ": "Giai đoạn tháng 1 – 6/2024 có ",
        " logged defect events, including ": " sự kiện lỗi đã ghi nhận, trong đó có ",
        " Critical events. Observed repair cost is ": " lỗi nghiêm trọng (Critical). Chi phí sửa chữa đã ghi nhận là ",
        " in unspecified local currency, with ": " theo đơn vị tiền tệ của nguồn chưa xác định loại tiền, với ",
        "% cost coverage.": "% tỷ lệ sự kiện có chi phí hợp lệ.",
        "Counts refer to logged events, not distinct defective units. Unknown costs remain unknown. Actual production and inspection exposure, unit identifiers, reporting completeness and causal attribution are unavailable.": "Các số đếm phản ánh sự kiện lỗi đã ghi nhận, không phải số sản phẩm lỗi riêng biệt. Chi phí chưa xác định vẫn được giữ là chưa xác định. Chưa có sản lượng và số lượng kiểm tra thực tế, mã định danh từng sản phẩm, bằng chứng về mức độ đầy đủ của việc ghi nhận hoặc cơ sở kết luận nhân quả.",
        "| Disposition | Count |": "| Kết quả xử lý dữ liệu | Số lượng |",
        "raw_equals_removed_plus_quarantined_plus_clean": "Đối soát số dòng nguồn bằng dòng trùng đã bỏ cộng dòng cách ly cộng sự kiện sạch",
        "duplicate_copies_removed": "Bản sao trùng đã loại bỏ",
        "retained_clean_events": "Sự kiện sạch được giữ lại",
        "out_of_scope_events": "Sự kiện ngoài phạm vi phân tích",
        "quarantined_rows": "Dòng dữ liệu cách ly",
        "analysis_events": "Sự kiện trong phạm vi phân tích",
        "raw_rows": "Dòng dữ liệu nguồn",
        "Verified": "Đã kiểm chứng",
        "Failed": "Không đạt",
        "The completed workbook documents observed issues, row-level audit lineage, assumptions and before/after impacts. Issue counts overlap and must not be added to infer removals.": "Tệp Excel hoàn chỉnh ghi lại vấn đề đã phát hiện, nguồn gốc kiểm tra đến từng dòng, giả định và tác động trước/sau xử lý. Số lượng các vấn đề có thể trùng nhau; không cộng chúng để suy ra số dòng bị loại.",
        "Part Three: trend": "Phần 3: xu hướng",
        "Logged events changed from ": "Số sự kiện lỗi đã ghi nhận thay đổi từ ",
        " in January to ": " trong tháng Một lên ",
        " in June, an increase of ": " trong tháng Sáu, tăng ",
        "%. This is a negative workload signal that warrants investigation; it does not establish causal deterioration without actual output and inspection coverage.": "%. Đây là dấu hiệu gia tăng gánh nặng xử lý cần điều tra; chưa thể kết luận chất lượng sản xuất suy giảm khi thiếu sản lượng thực tế và thông tin về phạm vi kiểm tra.",
        "| Month metadata | Events | Critical | Known repair cost |": "| Tháng | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Chi phí sửa chữa đã ghi nhận |",
        "Location and severity": "Vị trí lỗi và mức độ nghiêm trọng",
        "| Location | Events | Critical | Critical share |": "| Vị trí | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Tỷ trọng nghiêm trọng |",
        "Surface has the most events; Component has the most Critical events and the highest Critical share, so focus failure review there. Exploratory association p-value is ": "Bề mặt có nhiều sự kiện nhất; linh kiện có nhiều lỗi nghiêm trọng (Critical) nhất và tỷ trọng nghiêm trọng cao nhất, nên ưu tiên rà soát hỏng hóc tại đây. Giá trị p của kiểm định mối liên hệ mang tính thăm dò là ",
        " and Cramer's V is ": " và hệ số V của Cramér là ",
        ". Expected-cell checks pass for this source; the association is small and does not prove a causal mechanism.": ". Dữ liệu này đạt kiểm tra về tần số kỳ vọng trong các ô; mức liên hệ nhỏ và không chứng minh cơ chế nhân quả.",
        "Other notable patterns and beyond-brief insights": "Các mẫu hình đáng chú ý và phát hiện ngoài yêu cầu",
        "Workload and exposure rankings differ: ": "Thứ hạng theo số sự cố lỗi và sản lượng tham chiếu khác nhau: ",
        " leads reference-output intensity at ": " có cường độ lỗi theo sản lượng tham chiếu cao nhất, ở mức ",
        " logged events per thousand reference units. Verify actual exposure and product mix before assigning line causality.": " sự kiện lỗi đã ghi nhận trên mỗi nghìn đơn vị tham chiếu. Cần xác minh sản lượng thực tế và cơ cấu sản phẩm trước khi quy nguyên nhân cho chuyền.",
        " accounts for ": " chiếm ",
        " observed repair cost and ": " chi phí sửa chữa đã ghi nhận và ",
        "% of known cost. Maintenance and Finance should review repair mechanisms together with missing-cost capture.": "% tổng chi phí đã ghi nhận. Bộ phận Bảo trì và Tài chính nên cùng rà soát cách sửa chữa và việc bổ sung chi phí còn thiếu.",
        "Known repair burden is distributed: reaching the Pareto threshold requires ": "Chi phí sửa chữa đã ghi nhận phân tán trên nhiều sản phẩm: để đạt ngưỡng Pareto cần ",
        " master products. Do not assume a generic eighty/twenty rule. Calendar coverage also leaves ": " sản phẩm trong danh mục chuẩn. Không mặc định áp dụng quy tắc 80/20. Đồng thời có ",
        " dates without logged events, so confirm reporting coverage before daily alerts.": " ngày không có sự kiện được ghi nhận; cần xác nhận mức độ đầy đủ của việc ghi nhận trước khi phát cảnh báo hằng ngày.",
        "Sensitivity of conclusions": "Độ nhạy của kết luận",
        "Only genuinely ambiguous slash dates are changed. The alternative interpretation and withholding scenarios change the in-scope evidence population, so lower totals are not savings or manufacturing improvement. These scenarios are not confidence intervals.": "Chỉ những ngày viết bằng dấu gạch chéo thực sự chưa rõ thứ tự tháng/ngày mới được thay đổi. Kịch bản diễn giải theo thứ tự còn lại và kịch bản tạm loại các dòng này làm thay đổi tập dữ liệu trong phạm vi phân tích, nên tổng số thấp hơn không thể hiện tiết kiệm hoặc cải thiện sản xuất. Các kịch bản này không phải khoảng tin cậy.",
        "| Scenario | Events | Critical | Known cost | January-June count change | Historical candidates |": "| Kịch bản | Sự kiện lỗi đã ghi nhận | lỗi nghiêm trọng (Critical) | Chi phí đã ghi nhận | Thay đổi số sự kiện từ tháng 1 đến tháng 6 | Ngày cần kiểm tra trong lịch sử |",
        "primary_md": "Chính: tháng/ngày",
        "ambiguous_only_dm_stress": "Thử độ nhạy: đảo ngày/tháng của ngày chưa rõ",
        "withhold_ambiguous_raw_rows": "Tạm loại dòng có ngày chưa rõ",
        "Component Critical burden, Line Three reference intensity and Functional known-cost priorities remain stable across these declared scenarios. Exact product ordering and historical candidate dates change. Confirm date convention with the source owner; candidate totals do not measure detector accuracy.": "Các ưu tiên về lỗi nghiêm trọng (Critical) ở linh kiện, cường độ tham chiếu của Chuyền 3 và chi phí đã ghi nhận của lỗi chức năng vẫn giữ nguyên trong các kịch bản đã nêu. Thứ tự sản phẩm cụ thể và các ngày lịch sử cần kiểm tra thay đổi. Cần xác nhận quy ước ngày với bên quản lý dữ liệu nguồn; số ngày được sàng lọc không đo độ chính xác của bộ phát hiện.",
        "Part Four: priorities, owners and success measures": "Phần 4: ưu tiên, người phụ trách và thước đo kết quả",
        "Investigate Critical defects at Component": "Điều tra lỗi nghiêm trọng (Critical) tại linh kiện",
        "Investigate Critical defects at ": "Điều tra lỗi nghiêm trọng (Critical) tại ",
        "Review these defect records with operators, inspect representative failures and verify causes before choosing corrective actions.": "Cùng nhân viên vận hành rà soát các bản ghi lỗi, mổ xẻ mẫu lỗi thực tế (failure teardown) và xác minh nguyên nhân trước khi chọn biện pháp khắc phục.",
        "Quality Engineer and relevant Production Team Leader": "Kỹ sư Chất lượng và Trưởng nhóm Sản xuất liên quan",
        "Track Critical events and their share at this location with actual inspected/produced units and comparable inspection coverage; set a target after baseline validation.": "Theo dõi số lỗi nghiêm trọng (Critical) và tỷ trọng tại vị trí này, kèm số lượng kiểm tra/sản xuất thực tế và phạm vi kiểm tra tương đương; đặt mục tiêu sau khi xác minh đường cơ sở.",
        "Validate the burden on Line Three": "Xác minh gánh nặng lỗi trên Chuyền 3",
        "Validate the burden on ": "Xác minh gánh nặng lỗi trên ",
        "Compare product and defect mix, confirm actual output and inspection coverage, then investigate recurring failures with the line team.": "So sánh cơ cấu sản phẩm và loại lỗi, xác nhận sản lượng thực tế cùng phạm vi kiểm tra, rồi phối hợp với nhóm vận hành chuyền điều tra hỏng hóc lặp lại.",
        "Line Three Team Leader with Production Planning": "Trưởng nhóm Chuyền 3 phối hợp với bộ phận Kế hoạch Sản xuất",
        " Team Leader with Production Planning": ": Trưởng nhóm phối hợp với bộ phận Kế hoạch Sản xuất",
        "Recalculate events per actual produced/inspected units and repeat Critical-event counts at matched exposure; no reduction target is inferred from this case.": "Tính lại số sự kiện theo số lượng sản xuất/kiểm tra thực tế và theo dõi lại số lỗi nghiêm trọng (Critical) trên cơ sở quy mô tương đương; không suy ra mục tiêu giảm lỗi từ bộ dữ liệu này.",
        "Review Functional repair burden and cost capture": "Rà soát chi phí sửa chữa lỗi chức năng và việc ghi nhận chi phí",
        "Investigate high-intensity Category 4 products": "Điều tra sản phẩm Nhóm 4 có cường độ lỗi cao",
        "Investigate high-intensity ": "Điều tra nhóm sản phẩm có cường độ lỗi cao: ",
        "Investigate repair cost per event on ": "Điều tra chi phí sửa chữa mỗi sự kiện tại ",
        " Team Leader with Maintenance and Finance": ": Trưởng nhóm phối hợp với bộ phận Bảo trì và Tài chính",
        "Compare design, materials and process steps of the highest-intensity products in this category with lower-intensity products on the same line; review representative failures before changing the process.": "So sánh thiết kế, vật liệu và các bước công đoạn của những sản phẩm có cường độ lỗi cao nhất trong nhóm này với sản phẩm cường độ thấp hơn trên cùng chuyền; mổ xẻ mẫu lỗi thực tế (failure teardown) trước khi thay đổi quy trình.",
        "Process Engineering with Quality Engineer and Production Team Leaders": "Kỹ thuật quy trình cùng Kỹ sư chất lượng và các Trưởng nhóm sản xuất",
        "Track this category's reference intensity on each line and its ratio to the rest of the line, with Critical share; set a reduction target after actual output is confirmed.": "Theo dõi cường độ lỗi tham chiếu của nhóm này trên từng chuyền và tỷ lệ so với phần còn lại của chuyền, kèm tỷ trọng lỗi nghiêm trọng (Critical); đặt mục tiêu giảm sau khi xác nhận sản lượng thực tế.",
        "Break down repair cost by repair route, labour, parts and rework with Maintenance; confirm cost-recording rules with Finance; compare the same category on other lines before attributing the gap to the line.": "Cùng bộ phận Bảo trì tách chi phí sửa chữa theo cách sửa, nhân công, vật tư và làm lại; xác nhận quy tắc ghi nhận chi phí với Tài chính; so sánh cùng nhóm sản phẩm trên các chuyền khác trước khi quy chênh lệch cho chuyền.",
        "Track average known repair cost per event on this line against other lines for the same category, with cost coverage; set a savings target only after repair-cost records are verified.": "Theo dõi chi phí sửa chữa trung bình mỗi sự kiện của chuyền này so với các chuyền khác trong cùng nhóm sản phẩm, kèm tỷ lệ sự kiện có chi phí hợp lệ; chỉ đặt mục tiêu tiết kiệm sau khi đã xác minh bản ghi chi phí sửa chữa.",
        " repair burden and cost capture": " và việc ghi nhận chi phí sửa chữa",
        "Review costly recurring repairs with Maintenance, reconcile missing/invalid costs with Finance, and verify corrective-action economics.": "Cùng bộ phận Bảo trì rà soát các sửa chữa lặp lại có chi phí cao, đối soát chi phí thiếu/không hợp lệ với Tài chính và xác minh hiệu quả kinh tế của biện pháp khắc phục.",
        "Maintenance Lead, Quality Engineer and Finance": "Trưởng bộ phận Bảo trì, Kỹ sư Chất lượng và bộ phận Tài chính",
        "Track known repair cost, cost per repaired event and cost coverage together; compare verified repair costs at comparable exposure before setting a savings target.": "Theo dõi đồng thời chi phí sửa chữa đã ghi nhận, chi phí mỗi sự kiện được sửa và tỷ lệ sự kiện có chi phí hợp lệ; so sánh chi phí được xác minh ở quy mô tương đương trước khi đặt mục tiêu tiết kiệm.",
        "Action: ": "Hành động: ",
        "Owner: ": "Phụ trách: ",
        "Success measure: ": "Thước đo kết quả: ",
        "Next capabilities and stop rule": "Khả năng cần bổ sung và điều kiện dừng",
        "Static views cannot confirm a cause, assign an accountable corrective action, validate reporting completeness or verify that a fix worked. Next add actual produced and inspected units with event-time provenance, source-quality contracts, operator feedback and acknowledged alert cases, action ownership, and comparable follow-up exposure. Keep human review before operational decisions.": "Màn hình tĩnh chưa đủ để xác nhận nguyên nhân, giao trách nhiệm khắc phục, xác minh mức độ đầy đủ của việc ghi nhận hoặc kiểm tra hiệu quả sửa lỗi. Tiếp theo cần bổ sung số lượng sản xuất và kiểm tra thực tế có nguồn gốc theo thời điểm sự kiện, cam kết chất lượng dữ liệu nguồn, phản hồi từ vận hành, quy trình tiếp nhận cảnh báo, người phụ trách hành động và dữ liệu theo dõi sau can thiệp ở quy mô tương đương. Con người cần rà soát trước khi quyết định vận hành.",
        "This finite analysis stops after data quality, predefined sensitivity and the additional operational insights. Further analysis would add little without new exposure and reporting evidence. No model API spending occurs inside this deterministic helper.": "Phân tích hữu hạn này dừng sau khi kiểm tra chất lượng dữ liệu, thực hiện các kịch bản độ nhạy định trước và rút ra phát hiện vận hành bổ sung. Phân tích tiếp khó mang lại nhiều giá trị khi chưa có dữ liệu mới về sản lượng và mức độ đầy đủ của việc ghi nhận. Công cụ tính toán xác định này không gọi API mô hình có tính phí.",
        "Reproduction and evidence": "Tái lập và bằng chứng",
        "Run ": "Chạy ",
        ". Raw defect JSON is fetched over HTTP into memory and never saved. The master and workbook template are read-only inputs. ": ". JSON lỗi nguồn được lấy qua HTTP vào bộ nhớ và không lưu thành tệp. Danh mục sản phẩm và mẫu Excel là đầu vào chỉ đọc. ",
        " contains aggregate tool results, source hashes and code hashes; ": " chứa kết quả tổng hợp của công cụ, mã băm nguồn và mã băm mã thực thi; ",
        " maps report claims to exact numeric paths. The runtime attaches its execution ID when recording the tool result in the run ledger.": " ánh xạ các số liệu trong báo cáo đến đúng đường dẫn giá trị. Hệ thống gắn mã lần thực thi khi ghi kết quả công cụ vào sổ bằng chứng của lượt chạy.",
        "Monthly logged events and Critical events": "Sự kiện lỗi đã ghi nhận và lỗi nghiêm trọng (Critical) theo tháng",
        "January": "Tháng Một",
        "February": "Tháng Hai",
        "March": "Tháng Ba",
        "April": "Tháng Tư",
        "May": "Tháng Năm",
        "June": "Tháng Sáu",
        "Line One": "Chuyền 1",
        "Line Two": "Chuyền 2",
        "Line Three": "Chuyền 3",
        "Line Four": "Chuyền 4",
        "Line 1": "Chuyền 1",
        "Line 2": "Chuyền 2",
        "Line 3": "Chuyền 3",
        "Line 4": "Chuyền 4",
        "Line-1": "Chuyền 1",
        "Line-2": "Chuyền 2",
        "Line-3": "Chuyền 3",
        "Line-4": "Chuyền 4",
        "logged defect events": "sự kiện lỗi đã ghi nhận",
        "Logged defect events": "Sự kiện lỗi đã ghi nhận",
        "Critical events": "lỗi nghiêm trọng (Critical)",
        "critical events": "lỗi nghiêm trọng (Critical)",
        "Critical Events": "Lỗi nghiêm trọng (Critical)",
        "containment check": "chốt kiểm soát khoanh vùng tức thời (containment)",
        "Containment check": "Chốt kiểm soát khoanh vùng tức thời (containment)",
        "containment checks": "chốt kiểm soát khoanh vùng tức thời (containment)",
        "Containment checks": "Chốt kiểm soát khoanh vùng tức thời (containment)",
        "tear down representative failures": "mổ xẻ mẫu lỗi thực tế (failure teardown)",
        "Tear down representative failures": "Mổ xẻ mẫu lỗi thực tế (failure teardown)",
        "teardown representative failures": "mổ xẻ mẫu lỗi thực tế (failure teardown)",
        "Teardown representative failures": "Mổ xẻ mẫu lỗi thực tế (failure teardown)",
        "cost coverage": "tỷ lệ sự kiện có chi phí hợp lệ",
        "Cost coverage": "Tỷ lệ sự kiện có chi phí hợp lệ",
        "January through June of year twenty twenty-four": "giai đoạn tháng 1 – 6/2024",
        "January through June of 2024": "giai đoạn tháng 1 – 6/2024",
        "January to June 2024": "giai đoạn tháng 1 – 6/2024",
        "Surface": "Bề mặt",
        "Component": "Linh kiện",
        "Internal": "Bên trong",
        "Functional": "Chức năng",
        "Structural": "Kết cấu",
        "Cosmetic": "Ngoại quan",
        "Critical": "Nghiêm trọng",
        " events; ": " sự kiện; ",
        " has ": " có ",
        "Review ": "Rà soát ",
    }
)

_PROTECTED = re.compile(r"\[claim:((?:[^\[\]]|\[\d+\])+)\]|`[^`]*`|\]\([^)]*\)")
_NUMBERS = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def _display_numbers(text):
    """Normalize known metadata wording without exempting evidence from checks."""
    aliases = {
        "January through June of year twenty twenty-four": "1 6/2024",
        "January through June of 2024": "1 6/2024",
        "January to June 2024": "1 6/2024",
        "January-June": "1 6",
        "Line One": "Line 1",
        "Line Two": "Line 2",
        "Line Three": "Line 3",
        "Line Four": "Line 4",
        "Part Three": "Part 3",
        "Part Four": "Part 4",
        "eighty/twenty": "80/20",
    }
    for source, target in aliases.items():
        text = text.replace(source, target)
    text = re.sub(r"\bLine[-_ ]+(\d+)\b", r"Line \1", text)
    return _NUMBERS.findall(text)


def translate_text(text, language="en"):
    if normalize_language(language) == "en":
        return text
    protected = []

    def preserve(match):
        protected.append(match.group())
        return "\x00PROTECTED" + str(len(protected) - 1) + "\x00"

    rendered = _PROTECTED.sub(preserve, text)

    translated_phrases: list[tuple[str, str, str]] = []
    for source, target in sorted(PHRASES.items(), key=lambda pair: len(pair[0]), reverse=True):
        if source in rendered:
            token = f"\x00PHRASE_{len(translated_phrases)}\x00"
            translated_phrases.append((token, source, target))
            rendered = rendered.replace(source, token)

    check_text = rendered
    check_rendered = rendered
    for token, source, target in translated_phrases:
        rendered = rendered.replace(token, target)
        check_text = check_text.replace(token, source)
        check_rendered = check_rendered.replace(token, target)

    rendered = re.sub(
        r"\x00PROTECTED(\d+)\x00", lambda match: protected[int(match.group(1))], rendered
    )
    check_text = re.sub(
        r"\x00PROTECTED(\d+)\x00", lambda match: protected[int(match.group(1))], check_text
    )
    check_rendered = re.sub(
        r"\x00PROTECTED(\d+)\x00", lambda match: protected[int(match.group(1))], check_rendered
    )

    if _display_numbers(check_rendered) != _display_numbers(check_text):
        raise ValueError("Display translation changed numeric evidence tokens")
    return rendered


def localize_report(text, language="en"):
    rendered = translate_text(text, language)
    return (
        VI_NUMBER_NOTE + "\n\n" + rendered
        if language == "vi" and VI_NUMBER_NOTE not in rendered
        else rendered
    )


def vietnamese_metadata(value):
    digits = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")

    def words(number):
        if number < 10:
            return digits[number]
        if number < 100:
            ten, unit = divmod(number, 10)
            prefix = "mười" if ten == 1 else digits[ten] + " mươi"
            return prefix + (
                " " + ("lăm" if unit == 5 else "mốt" if unit == 1 and ten > 1 else digits[unit])
                if unit
                else ""
            )
        for scale, name in ((10**9, "tỷ"), (10**6, "triệu"), (1000, "nghìn"), (100, "trăm")):
            if number >= scale:
                first, rest = divmod(number, scale)
                join = " lẻ " if scale == 100 and rest < 10 else " "
                return words(first) + " " + name + (join + words(rest) if rest else "")

    months = (
        "Một",
        "Hai",
        "Ba",
        "Tư",
        "Năm",
        "Sáu",
        "Bảy",
        "Tám",
        "Chín",
        "Mười",
        "Mười Một",
        "Mười Hai",
    )
    text = str(value)
    month = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if month and 1 <= int(month.group(2)) <= 12:
        return "tháng " + months[int(month.group(2)) - 1] + " năm " + words(int(month.group(1)))
    line = re.fullmatch(r"Line[-_ ]+(\d+)", text, re.IGNORECASE)
    if line:
        return "Dây chuyền " + words(int(line.group(1)))
    rendered = re.sub(r"\d+", lambda match: " " + words(int(match.group())) + " ", text)
    rendered = re.sub(r"[ \t]+", " ", rendered).strip()
    return translate_text(rendered, "vi")
