# Manufacturing Quality

[English](README.md) | Tiếng Việt

Phân tích lỗi sản xuất giai đoạn tháng 1–6/2024, so sánh các chuyền và nhóm sản phẩm, đồng thời tra cứu từng bản ghi đứng sau mỗi chỉ số. Ứng dụng cho bài case study RFID Data Analyst kết hợp **Dashboard** trực quan có bộ lọc với không gian làm việc **Agent** chạy trên cùng hệ thống tính toán Python xác định.

Backend Python tải dữ liệu lỗi qua HTTP, làm sạch và đối chiếu với danh mục sản phẩm chuẩn, rồi tính toán các KPI, kiểm định thống kê và kịch bản độ nhạy. Frontend React + TypeScript hiển thị kết quả trên cả hai tab, trong khi [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) và báo cáo tự động bên dưới bao phủ trọn vẹn bài nộp từ Part 1 đến Part 4.

## Cài đặt và chạy

Sử dụng **Python 3.12** và **Node.js 24 cùng npm**. Clone repo rồi chạy script cài đặt từ thư mục gốc:

```sh
git clone https://github.com/delatran/manufacturing-quality.git
cd manufacturing-quality
```

Script tạo `.venv`, cài đặt lockfile Python có kiểm tra hash (`pip --require-hashes`), chạy `npm ci` và build giao diện frontend. Nếu chưa có `.env`, script sẽ sao chép từ `.env.example`. Có thể thêm `-RuntimeOnly` trên Windows hoặc `--runtime-only` trên macOS/Linux nếu chỉ cần cài dependency Python phục vụ chạy ứng dụng.

Windows PowerShell:

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

macOS hoặc Linux:

```sh
sh scripts/setup.sh
sh scripts/start.sh
```

Mở [http://127.0.0.1:8000](http://127.0.0.1:8000). Một tiến trình duy nhất phục vụ cả giao diện đã build lẫn API FastAPI. Tài liệu API tương tác nằm tại [/docs](http://127.0.0.1:8000/docs) và trạng thái hệ thống nằm tại [/health](http://127.0.0.1:8000/health). Nhấn Ctrl+C để dừng server.

Dashboard hoạt động đầy đủ mà không cần OpenAI key (chỉ cần kết nối internet để tải bản ghi lỗi của đề bài qua HTTP). Để bật Agent, khai báo hai biến môi trường trong `.env` cục bộ hoặc môi trường tiến trình:

```dotenv
OPENAI_API_KEY=your_server_side_key
OPENAI_MODEL=gpt-6-luna
```

Ứng dụng sử dụng model `gpt-6-luna` qua OpenAI Agents API và Codex harness (`analyst/config.py`), kết nối model với bộ công cụ phân tích Python cục bộ và cơ chế kiểm duyệt báo cáo tự động. Khi chưa cấu hình `OPENAI_API_KEY`, Dashboard vẫn hoạt động bình thường còn Agent ở trạng thái tắt.

Cấu hình đọc `OPENAI_API_KEY` và `OPENAI_MODEL` (ưu tiên biến môi trường tiến trình hơn file `.env`). Chính sách chạy trong `Settings` (`analyst/config.py`) bật managed Agents API với mức suy luận tối đa và `unbounded_runs=True` ở cả local và Cloudflare. Ứng dụng không đặt trần vòng lặp, token đầu ra, số lần gọi công cụ, chi phí hoặc thời gian mỗi lượt; dữ liệu gốc vẫn ở chế độ chỉ đọc. Giới hạn của nhà cung cấp và hạ tầng vẫn áp dụng. Cấu hình giới hạn hữu hạn được giữ cho fixture kiểm thử.

Chỉ đặt `OPENAI_API_KEY` ở môi trường backend cục bộ hoặc secret của Cloudflare Worker—tuyệt đối không đưa vào bản build frontend. Với [triển khai Cloudflare](cloudflare/README.md) ([tranthiennhan.com/manufacturing-quality](https://tranthiennhan.com/manufacturing-quality)), Worker giữ khóa API và chuyển tiếp yêu cầu từ Container cô lập của từng người dùng nên khóa không bao giờ nằm trong Container.

Khi phát triển giao diện frontend, chạy `npm --prefix frontend run dev` trong lúc backend đang lắng nghe tại `127.0.0.1:8000`.

Bridge Cloudflare chuyển tiếp managed sessions, sự kiện trực tiếp, kết quả công cụ, điều hướng native và huỷ lượt qua kết nối riêng. Mỗi phiên provider thuộc về một người dùng; việc gửi lại cùng một thông điệp giữ nguyên idempotency key. Xem [cấu hình cloud](cloudflare/README.md) để kiểm tra triển khai và khôi phục phiên.

## Khám phá Dashboard

Bắt đầu từ phần tổng quan, chọn một hạng mục ưu tiên, sau đó mở bằng chứng chi tiết hoặc lọc biểu đồ. Năm phân mục trên Dashboard tương ứng với các câu hỏi phân tích trọng tâm:

| Phân mục | Nội dung kiểm tra |
|---|---|
| **Tổng quan và ưu tiên** | Bốn chỉ số chính, đường xu hướng theo tháng, so sánh hai nửa giai đoạn và ba hạng mục ưu tiên kèm bằng chứng cùng hành động đề xuất. |
| **Xu hướng và ngày bất thường** | Số sự kiện lỗi và lỗi Critical theo tháng, các nhóm đóng góp chính vào mức tăng, và số lỗi từng ngày so với ngưỡng sàng lọc tính từ tháng 1. Nhấn vào ngày vượt ngưỡng để xem từng bản ghi. |
| **Vị trí và mức độ lỗi** | Tỷ trọng mức độ nghiêm trọng giữa các vị trí lỗi, bảng chéo kèm kiểm định chi-square / Cramér's V, và lượng lỗi theo phương pháp kiểm tra. |
| **Chuyền và sản phẩm** | Xếp hạng chuyền và sản phẩm theo số sự kiện hoặc số sự kiện trên 1.000 đơn vị sản lượng tham chiếu, kết hợp so sánh nhóm sản phẩm trong từng chuyền để tách ảnh hưởng của cơ cấu sản phẩm. |
| **Chi phí sửa chữa** | Chi phí sửa chữa hợp lệ theo chuyền, loại lỗi và mức độ nghiêm trọng, phân phối chi phí và các sản phẩm chiếm tỷ trọng chi phí cao nhất. |

Lọc dữ liệu theo tháng, chuyền, nhóm sản phẩm, sản phẩm, mức độ nghiêm trọng, loại lỗi, vị trí lỗi và phương pháp kiểm tra. Nút **Xem bằng chứng** chuyển thẳng tới biểu đồ hoặc bảng số liệu liên quan; nút **Tra cứu danh sách bản ghi lỗi** mở bảng dữ liệu đã làm sạch kèm truy vết về dòng HTTP gốc và quy tắc xử lý.

Menu **Xuất dữ liệu & DATA_QUALITY.xlsx** cho phép tải CSV theo bộ lọc đang chọn hoặc trọn bộ workbook [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) của đề bài.

**Lưu ý khi đọc chỉ số:**
- **Sự kiện trên 1.000 đơn vị tham chiếu** dùng `monthly_output` tĩnh trong bảng master nhân với số tháng đang chọn. Đây là cường độ lỗi trên sản lượng tham chiếu, không phải tỷ lệ lỗi trên sản lượng thực tế hay số lượng kiểm tra thực tế.
- **Chi phí sửa chữa đã ghi nhận** chỉ cộng các bản ghi có chi phí hợp lệ và luôn hiển thị rõ độ phủ; chi phí bị thiếu không bao giờ được gán bằng 0.

## Điều tra với Agent

Tab **Agent** kế thừa phạm vi đang chọn trên Dashboard và hiển thị rõ kỳ phân tích, bộ lọc, bốn chỉ số chính cùng ba câu hỏi gợi ý (`Câu 1`–`Câu 3`). Mỗi lượt hỏi cố định ngữ cảnh ngay khi gửi để các câu hỏi nối tiếp giữ nguyên phạm vi so sánh kể cả khi bộ lọc bên Dashboard thay đổi sau đó. Các nút thao tác trong câu trả lời của Agent có thể mở trực tiếp bằng chứng trên Dashboard hoặc áp dụng bộ lọc mới kèm nút hoàn tác.

Agent hiển thị trực tiếp tiến trình gọi công cụ, tóm tắt suy luận, đối chiếu từng con số với kết quả công cụ và xuất báo cáo kèm biểu đồ nội tuyến ngay trong khung hội thoại. Với các câu hỏi đào sâu, Agent kết hợp các phân mục chuẩn của Dashboard với truy vấn SQL chỉ đọc trên bảng sự kiện và bảng sản phẩm tham chiếu thuộc phạm vi đang xét—hỗ trợ phép nối tùy chỉnh, window function, kiểm tra chi tiết ngày bất thường và truy vết dòng dữ liệu gốc mà vẫn giữ nguyên định nghĩa mẫu số chuẩn.

Các câu hỏi gợi ý có thể thử ngay sau khi cấu hình `OPENAI_API_KEY`:

- “So sánh nửa đầu với nửa sau của kỳ này. Loại lỗi và chuyền nào đóng góp nhiều nhất vào mức tăng?”
- “Nhóm sản phẩm có cường độ lỗi cao nhất có còn cao hơn khi so trong từng chuyền không?”
- “Chuyền nào có chi phí sửa trung bình đã biết cao nhất, và dữ liệu chi phí của chuyền đó đầy đủ đến đâu?”

Luồng xử lý luôn hiển thị trong lúc Agent chạy. Gửi thêm chi tiết để điều hướng cùng lượt chạy, vẫn giữ nguyên phạm vi dữ liệu đã chốt. Câu trả lời và biểu đồ hiển thị ngay trong hội thoại. Agent không xuất file kết quả; Dashboard vẫn hỗ trợ CSV theo bộ lọc và workbook toàn bộ nghiên cứu.

Adapter managed nằm tại [analyst/managed_agents.py](analyst/managed_agents.py), các công cụ tại [analyst/product_tools.py](analyst/product_tools.py) và kiểm duyệt báo cáo tại [analyst/runtime.py](analyst/runtime.py). [Quy trình đánh giá](evals/README.md) và [nhật ký lịch sử](AUDIT.md) phân biệt kiểm tra tự động với kết quả gọi model thực tế theo từng thời điểm.

Cả hai tab hỗ trợ tiếng Anh và tiếng Việt, dùng chung phông chữ `Be Vietnam Pro` cùng bảng màu Warm Forest của `tranthiennhan.com`, và tự lưu ngôn ngữ đã chọn.

## Sinh lại và kiểm tra bản nộp

Script dưới đây tải dữ liệu lỗi qua HTTP, đối chiếu mã băm SHA-256 của dữ liệu nguồn/master/template với commit đề bài, tính toán snapshot chuẩn và cập nhật phần phân tích ở cả hai file README cùng [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx):

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_submission
.\.venv\Scripts\python.exe -m scripts.generate_submission --check
```

Trên macOS/Linux dùng `.venv/bin/python`. Thêm `--output-dir artifacts/submission-preview` nếu muốn xuất bản xem trước ra thư mục riêng. Cờ `--check` kiểm tra tính nhất quán giữa snapshot đã lưu, mã băm code tính toán, dữ liệu nguồn cục bộ, nội dung phân tích song ngữ trên README và các ô trong workbook mà không cần gọi mạng.

Để chạy độc lập toàn bộ pipeline phân tích và xuất gói bằng chứng:

```powershell
.\.venv\Scripts\python.exe -m analyst.case_study --output .runtime/manual-case/artifacts
```

Chạy toàn bộ bộ kiểm tra chất lượng mã nguồn từ thư mục gốc:

```powershell
.\scripts\check.ps1
```

Trên macOS/Linux dùng `sh scripts/check.sh`. Lệnh này kiểm tra lockfile dependency, Ruff format/lint, Mypy, pytest, tính toàn vẹn của gói nộp bài, cũng như Prettier, ESLint, TypeScript, bộ test giao diện và bản build frontend.

<!-- BEGIN GENERATED CANONICAL ANALYSIS -->

## Phân tích đề bài: Part 3

Phạm vi: tháng 1–6/2024 theo canonical policy `canonical-v2`, dataset `dataset-4586b9cf07494f8c8481a2f8f8a578359fbab7c4c7435b06ccb7defef31fa515` và snapshot báo cáo `2b9bd4ef427437f7c79678cc`. Dữ liệu lỗi được lấy qua HTTP lúc `2026-10-17T17:58:26.052168+00:00`. Tiền tệ là **local currency (chưa xác định loại tiền)**. Số chưa làm tròn, định nghĩa và bằng chứng nguồn nằm trong [snapshot chuẩn](reports/canonical_snapshot.json); dashboard hiển thị đúng các số này.

### Phát hiện chính

1. **Lỗi đang tăng: tín hiệu tiêu cực với nhà máy.** Số sự kiện lỗi tăng từ 147 trong tháng 1 lên 252 trong tháng 6 (+71,43%). Giai đoạn tháng 4–tháng 6 ghi nhận 657 sự kiện so với 414 của tháng 1–tháng 3 (+58,70%); giữa hai nửa giai đoạn, lỗi Critical tăng 52,90% và chi phí sửa chữa ghi nhận tăng 72,10%.
2. **Surface có nhiều lỗi nhất; Component có nhiều lỗi nghiêm trọng nhất.** Surface có 384 sự kiện. Component có 148 lỗi Critical trên 346 sự kiện (42,77%), trong khi các vị trí còn lại gộp chung chỉ 27,72%.
3. **Category 4 có cường độ lỗi tham chiếu cao nhất.** Nhóm này có 4,592 sự kiện trên 1.000 đơn vị sản lượng tham chiếu, so với 1,473 của toàn bộ sản phẩm còn lại. Trên cả 4 chuyền có sản xuất nhóm này, cường độ lỗi của nhóm gấp 1,56–7,19 lần phần còn lại của chuyền. 4 sản phẩm có cường độ cao nhất (81, 83, 82, 84) đều thuộc Category 4.
4. **Sửa lỗi ở Line-3 đắt gấp 1,66 lần.** Chi phí trung bình mỗi lần sửa có ghi nhận chi phí của Line-3 là 685,69, so với 413,35 ở các chuyền khác; vì vậy chuyền này chiếm 37,43% chi phí sửa chữa đã biết dù chỉ có 26,51% số sự kiện.
5. **Mức tăng tập trung.** Trong +105 sự kiện tăng thêm từ tháng 1 đến tháng 6, lỗi Cosmetic đóng góp +58 (55,2%) và Line-3 đóng góp +43 (41,0%).

### Chất lượng dữ liệu và đơn vị đếm

- Nhận 1.090 dòng thô qua HTTP. Làm sạch loại 14 bản dư (12 bản giống hệt, 1 bản giống sau chuẩn hóa nhãn, 1 bản trùng chỉ khác chi phí bị thiếu) và cách ly 2 dòng cùng ID nhưng mâu thuẫn, còn 1.074 sự kiện. 3 sự kiện hợp lệ nằm ngoài tháng 1–6/2024, nên **phân tích dùng 1.071 sự kiện**.
- 15 dòng được sửa (13 lỗi chữ hoa/khoảng trắng ở nhãn, 2 ngày rõ ràng dạng ngày/tháng). 426 ngày dạng gạch chéo đọc được theo hai cách; policy theo quy ước M/D/YYYY chiếm đa số và có scenario thử cách đọc ngược lại. 436 sự kiện được giữ có ít nhất một cờ; cờ và sửa đổi chồng lấp nên không cộng dồn.
- 4 sự kiện tham chiếu product không có trong master; chúng vẫn được tính vào tổng nhà máy nhưng không vào cường độ theo sản lượng. 22 sự kiện không có chi phí hợp lệ (độ phủ 97,95%); chi phí thiếu hoặc âm không bao giờ được điền ước tính. 12 dòng thô có chi phí đúng bằng 10, giá trị nhỏ nhất; chúng vẫn hợp lệ và có scenario tạm loại chúng.
- Không tháng nào có sự kiện sau ngày 28, và 16/182 ngày lịch không có sự kiện nào. Mức đầy đủ của việc ghi nhận chưa được xác minh, nên không được hiểu các khoảng trống cuối tháng là không có lỗi.
- Mọi vấn đề, cách phát hiện, cách xử lý và tác động nằm trong [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx).

### Q1. Xu hướng tháng 1–6/2024

| Tháng | Sự kiện lỗi | Critical | Chi phí đã ghi nhận | Có chi phí / tổng | Chi phí TB mỗi sự kiện có chi phí |
|---|---|---|---|---|---|
| 2024-01 | 147 | 50 | 65.488,29 | 142/147 | 461,19 |
| 2024-02 | 119 | 38 | 54.376,13 | 117/119 | 464,75 |
| 2024-03 | 148 | 50 | 67.147,84 | 144/148 | 466,30 |
| 2024-04 | 195 | 57 | 94.773,56 | 192/195 | 493,61 |
| 2024-05 | 210 | 80 | 103.148,47 | 209/210 | 493,53 |
| 2024-06 | 252 | 74 | 123.930,10 | 245/252 | 505,84 |

**Trả lời: tín hiệu tiêu cực.** Số sự kiện lỗi tăng 71,43% từ tháng 1 (147) đến tháng 6 (252). Ba tháng cuối có số sự kiện cao hơn rõ rệt: tháng 4–tháng 6 trung bình 219 sự kiện mỗi tháng so với 138 của tháng 1–tháng 3 (+58,70%). Không tháng nào có sự kiện sau ngày 28, nên cần kiểm tra mức đầy đủ của việc ghi nhận trước khi giải thích mức giảm tháng 2 hoặc coi mức tăng là thay đổi của quy trình sản xuất. Lỗi Critical (+52,90%) và chi phí sửa chữa ghi nhận (+72,10%) tăng cùng số lỗi; chi phí trung bình mỗi lần sửa có chi phí tăng từ 464,05 lên 498,22.

Mọi chuyền đều tăng. Line-3 tăng nhanh nhất (31 → 74 sự kiện) và đóng góp 41,0% mức tăng từ tháng 1 đến tháng 6; theo loại lỗi, Cosmetic đóng góp 55,2% (39 → 97). Mức tăng trên toàn nhà máy gợi ý kiểm tra trước các nguyên nhân chung như sản lượng, vật liệu hoặc thay đổi kiểm tra. Sản lượng tham chiếu cố định nên cường độ theo sản lượng tăng cùng tỷ lệ; cần sản lượng thực tế và số đơn vị được kiểm theo tháng để khẳng định tỷ lệ lỗi tăng, chứ không chỉ sản lượng hay khối lượng kiểm tra tăng.

### Q2. Vị trí lỗi và mức độ nghiêm trọng

| Vị trí | Sự kiện | Tỷ trọng sự kiện | Minor | Moderate | Critical | Tỷ trọng Critical tại vị trí |
|---|---|---|---|---|---|---|
| Surface | 384 | 35,85% | 157 | 127 | 100 | 26,04% |
| Component | 346 | 32,31% | 87 | 111 | 148 | 42,77% |
| Internal | 341 | 31,84% | 117 | 123 | 101 | 29,62% |

**Trả lời:** Surface là vị trí có nhiều lỗi nhất (384 sự kiện). Mức độ nghiêm trọng phụ thuộc vị trí: tỷ trọng Critical của Component là 42,77%, so với 27,72% ở các vị trí khác. Mối liên hệ có ý nghĩa thống kê rõ (chi-square p = 0,000003; điều kiện tần suất kỳ vọng đạt) nhưng mức độ nhỏ (Cramér's V = 0,121).

**Nên tập trung vào đâu:** bắt đầu phân tích hỏng hóc với lỗi Critical tại Component, vì vị trí này chiếm 42,41% tổng số lỗi Critical. Surface là vấn đề về số lượng với tỷ trọng Critical thấp nhất, nên phù hợp với biện pháp khoanh vùng và kiểm tra tiêu chuẩn hơn là phân tích hỏng hóc sâu. Nhãn vị trí không chỉ ra cơ chế hỏng; cần tháo mẫu lỗi đại diện và dữ liệu công đoạn để kiểm chứng nguyên nhân.

### Q3. Các mẫu hình đáng chú ý khác

| Mẫu hình | Bằng chứng | Vì sao quan trọng với team Production |
|---|---|---|
| Category 4 có cường độ lỗi cao nhất trên mọi chuyền | 4,592 so với 1,473 sự kiện trên 1.000 đơn vị tham chiếu; gấp 1,56–7,19 lần phần còn lại của từng chuyền. Không dùng mẫu số sản lượng: 14,1 sự kiện mỗi sản phẩm so với 9,4–10,2 ở các nhóm khác. Đây là sản phẩm sản lượng thấp (trung bình 512 đơn vị/tháng so với 991 của toàn nhà máy). | Vấn đề đi theo dòng sản phẩm chứ không theo chuyền, nên sửa ở một chuyền là không đủ. Đòn bẩy là thiết kế, vật liệu và cách chuyển đổi sản xuất; quy trách nhiệm theo từng chuyền sẽ bỏ sót chúng. |
| Chi phí sửa ở Line-3 cao hơn hẳn | 685,69 so với 413,35 mỗi lần sửa có chi phí (gấp 1,66 lần); riêng Category 4: 701,74 ở Line-3 so với 445,58 ở các chuyền khác. | Đây là đòn bẩy chi phí lớn nhất (37,43% chi phí sửa chữa đã biết). Vì chênh lệch vẫn còn trong cùng dòng sản phẩm, cần xem cách sửa và cách ghi nhận chi phí của chuyền, không phải cơ cấu sản phẩm. |
| Thứ hạng chuyền phụ thuộc vào mẫu số | Line-4 có nhiều sự kiện nhất (340); Line-3 có cường độ cao nhất (2,2907 trên 1.000 đơn vị tham chiếu). Category 2 chỉ được sản xuất trên Line-3; Category 3 chỉ được sản xuất trên Line-4. | Xếp hạng chuyền theo số lỗi thô dễ gây hiểu sai. Khi một nhóm sản phẩm chỉ có ở một chuyền thì không tách được tác động của chuyền và của sản phẩm, nên cần so cùng sản phẩm giữa các chuyền. |
| Chi phí sửa không đi theo mức độ nghiêm trọng | Chi phí trung bình mỗi sự kiện có chi phí: Minor 476,13, Moderate 490,25, Critical 489,14. Theo loại lỗi, Functional trung bình 503,85 so với 474,43–475,65. Trung vị chi phí đã biết 488,45, p90 770,35. | Không thể dùng chi phí thay cho mức độ nghiêm trọng: ưu tiên an toàn và chức năng theo số lỗi Critical, còn chi phí dùng để chọn việc cải tiến quy trình sửa. Chi phí gần như đồng đều cũng là lý do để kiểm tra với Tài chính cách ghi nhận chi phí sửa chữa. |
| Sàng lọc theo ngày chỉ ra các ngày có số lỗi cao bất thường | 7 ngày vượt ngưỡng 11,27 sự kiện/ngày tính từ tháng 1 (trung bình tháng 1 là 4,74): 2024-04-09, 2024-05-25, 2024-06-03, 2024-06-10, 2024-06-20, 2024-06-21, 2024-06-27. | Ngày vượt ngưỡng đầu tiên trong dữ liệu lịch sử rơi vào tháng 4. Sàng lọc hồi cứu chỉ ra các ngày cần điều tra; đây chưa phải cảnh báo thời gian thực hay bằng chứng xác nhận quy trình đã thay đổi. Cảnh báo vận hành cần baseline trượt theo từng chuyền và nhóm sản phẩm, dữ liệu ghi nhận đáng tin cậy và kiểm chứng với sự cố đã xác nhận. |
| Việc ghi nhận dữ liệu còn khoảng trống | Không có sự kiện sau ngày 28 ở bất kỳ tháng nào; 16/182 ngày trống; 22 chi phí chưa biết; 12 chi phí đúng bằng 10. | Cần sửa việc ghi nhận trước khi tự động hóa cảnh báo; nếu không, khoảng trống cuối tháng sẽ trông như cải thiện và tổng chi phí vẫn thiếu. |

**Đi sâu: sản phẩm 81–85.** Đây là năm mã sản phẩm đầu tiên của Category 4 và gồm bốn sản phẩm có cường độ cao nhất. Cả nhóm có 124 sự kiện, 47 Critical và 66.309,78 chi phí sửa chữa ghi nhận trên 15.438 đơn vị tham chiếu. Sự kiện theo tháng: 2024-01: 16; 2024-02: 8; 2024-03: 22; 2024-04: 23; 2024-05: 29; 2024-06: 26.

| ID / Sản phẩm | Sự kiện | Critical | Sự kiện / 1.000 tham chiếu | Chi phí ghi nhận | Có chi phí / tổng | Hạng cường độ (trên 100) |
|---|---|---|---|---|---|---|
| 81 / Product CC | 34 | 11 | 11,980 | 16.756,54 | 34/34 | 1 |
| 83 / Product CE | 26 | 9 | 11,807 | 14.180,07 | 25/26 | 2 |
| 82 / Product CD | 23 | 10 | 10,222 | 11.863,32 | 23/23 | 3 |
| 84 / Product CF | 22 | 8 | 6,236 | 11.340,20 | 21/22 | 4 |
| 85 / Product CG | 19 | 9 | 4,113 | 12.169,65 | 19/19 | 15 |

### Độ nhạy với giả định

| Scenario | Sự kiện | Chi phí ghi nhận | Có chi phí / tổng | Sự kiện SP 81–85 | Critical SP 81–85 | Chi phí SP 81–85 |
|---|---|---|---|---|---|---|
| Canonical | 1.071 | 508.864,39 | 1.049/1.071 | 124 | 47 | 66.309,78 |
| exclude_cost_10 | 1.071 | 508.754,39 | 1.038/1.071 | 124 | 47 | 66.309,78 |
| ambiguous_dm | 837 | 398.151,06 | 822/837 | 96 | 36 | 50.470,98 |
| year_2024 | 1.074 | 511.041,96 | 1.052/1.074 | 125 | 47 | 66.974,77 |

`exclude_cost_10` tạm loại các chi phí bằng 10 mà không đổi số sự kiện; `ambiguous_dm` đọc các ngày mơ hồ theo D/M; `year_2024` chuyển năm ngoài kỳ sang 2024. Mỗi scenario được tính riêng và không sửa kết quả canonical. Các tín hiệu dẫn đầu (vị trí nghiêm trọng nhất, chuyền và nhóm sản phẩm có cường độ cao nhất, chuyền có chi phí sửa cao nhất, loại lỗi và sản phẩm tốn chi phí nhất), và vì vậy cả ba ưu tiên, không đổi trong mọi scenario đã thử. Chênh lệch cho biết độ nhạy với giả định, không phải khoảng tin cậy hay khoản tiết kiệm.

## Part 4: Đề xuất

### Ba ưu tiên hàng đầu

Đây cũng là ba ưu tiên dashboard hiển thị ở mục “Ưu tiên hàng đầu cho phạm vi này” trong phần Tổng quan; backend suy ra chúng từ phân tích ở trên. Các mục tiêu là tạm thời cho đến khi có sản lượng và khối lượng kiểm tra thực tế.

#### Ưu tiên 1: Điều tra lỗi Critical tại Component

- **Bằng chứng:** 148/346 sự kiện tại Component là Critical (42,77%), so với 27,72% ở các vị trí khác; Component chiếm 42,41% tổng số lỗi Critical.
- **Hành động:** Lấy 148 bản ghi Critical, tháo và phân tích lỗi đại diện theo từng dòng sản phẩm, kiểm tra lô linh kiện đầu vào và thông số lắp ráp; thêm bước kiểm khoanh vùng tại khâu kiểm Component cho đến khi xác nhận nguyên nhân.
- **Phụ trách:** Kỹ sư Chất lượng (chủ trì) cùng Trưởng nhóm sản xuất của các chuyền liên quan.
- **Thước đo thành công:** Số và tỷ trọng Critical tại Component theo tháng. Mục tiêu tạm thời: đưa tỷ trọng về mức 27,72% của các vị trí khác trong hai tháng sau biện pháp khắc phục.

#### Ưu tiên 2: Điều tra sản phẩm Category 4 có cường độ lỗi cao

- **Bằng chứng:** 282 sự kiện trên 61.410 đơn vị tham chiếu (4,592 trên 1.000) so với 1,473 của các sản phẩm khác; cao hơn phần còn lại của chuyền ở 4/4 chuyền (gấp 1,56–7,19 lần); các sản phẩm 81, 83, 82, 84 dẫn đầu cường độ lỗi.
- **Hành động:** So sánh thiết kế, vật liệu và các bước công đoạn của Category 4 (kể cả việc chuyển đổi sản xuất ở các sản phẩm sản lượng thấp này) với các nhóm khác; bắt đầu từ các sản phẩm dẫn đầu và thử biện pháp trên một chuyền trước khi nhân rộng.
- **Phụ trách:** Kỹ thuật quy trình/sản phẩm (chủ trì) cùng Kỹ sư Chất lượng và trưởng nhóm của các chuyền sản xuất nhóm này.
- **Thước đo thành công:** Cường độ lỗi của Category 4 trên từng chuyền và tỷ lệ so với phần còn lại của chuyền. Mục tiêu tạm thời: giảm một nửa khoảng chênh 3,119 trên 1.000 trong một quý.

#### Ưu tiên 3: Điều tra chi phí sửa chữa mỗi sự kiện tại Line-3

- **Bằng chứng:** 685,69 mỗi lần sửa có chi phí so với 413,35 ở các chuyền khác (gấp 1,66 lần); 37,43% chi phí đã biết đến từ 26,51% số sự kiện; chênh lệch vẫn còn trong Category 4 (701,74 so với 445,58).
- **Hành động:** Cùng Bảo trì tách chi phí sửa của Line-3 theo nhân công, vật tư, làm lại và dịch vụ ngoài; so sánh cách sửa cùng sản phẩm ở chuyền khác; xác nhận với Tài chính rằng các chuyền ghi nhận chi phí theo cùng quy tắc.
- **Phụ trách:** Trưởng nhóm Line-3 (chủ trì) cùng Trưởng bộ phận Bảo trì và Tài chính.
- **Thước đo thành công:** Chi phí sửa trung bình mỗi sự kiện của Line-3 so với các chuyền khác, kèm độ phủ chi phí. Mục tiêu tạm thời: thu hẹp một nửa khoảng chênh 272,33 mỗi sự kiện trong một quý.

### Bước tiếp theo cho công cụ

Khi dùng hằng ngày, dashboard tĩnh vẫn bắt team Production tự phát hiện thay đổi, tự giải thích và tự theo dõi bằng tay. Bảng dưới đối chiếu từng hạn chế với những gì công cụ này đã làm và năng lực nên bổ sung tiếp.

| Hạn chế của dashboard tĩnh | Công cụ này đã làm được | Năng lực bổ sung tiếp theo |
|---|---|---|
| Phải tự mở ra xem; không ai được báo khi có thay đổi. | Phần Xu hướng và ngày bất thường đánh dấu các ngày lịch sử vượt ngưỡng tính từ tháng 1 (7 ngày trong toàn bộ kỳ đề bài); bộ lọc tính lại phạm vi hiển thị. Công cụ chưa gửi cảnh báo thời gian thực. | Kiểm tra theo lịch từng ca, chuyền và nhóm sản phẩm, tự đẩy cảnh báo kèm bằng chứng đến người phụ trách (Teams, Zalo hoặc e-mail). |
| Báo cáo tuần vẫn phải tổng hợp thủ công. | Menu xuất dữ liệu của Dashboard tải CSV theo bộ lọc đang áp dụng và workbook DATA_QUALITY.xlsx riêng cho toàn bộ đề bài. Backend có endpoint brief Markdown và ZIP bằng chứng; Dashboard hiện không có nút cho các đầu ra này. | Gửi bản tin theo lịch tới từng phòng ban, có bước duyệt và xác nhận đã nhận. |
| Câu hỏi đào sâu vẫn cần người phân tích. | Tab Agent điều tra câu hỏi bằng đúng các metric của backend và đối chiếu mọi con số với receipt của công cụ. Đây là bản prototype: cần OpenAI key và bộ kiểm thử live chưa đạt toàn bộ ([AUDIT.md](AUDIT.md)). | Làm Agent đủ nhanh và ổn định cho dùng hằng ngày, sau đó để nó tự soạn bản điều tra cho mỗi cảnh báo để người phụ trách duyệt. |
| Phát hiện không tự trở thành hành động. | Các khuyến nghị nêu hành động và vai trò phụ trách đề xuất. Giao diện hiện không có trình quản lý công việc theo dõi; bản ghi backend không chứng minh công việc đã được giao hay hoàn thành. | Theo dõi hành động khắc phục dùng chung, có hạn chót, escalation và đo trước/sau trên cùng định nghĩa. |
| Dữ liệu đến muộn và thiếu. | Lấy dữ liệu qua HTTP và làm mới thủ công; lần làm mới lỗi hoặc dữ liệu cũ đều được gắn nhãn. | Nguồn MES/ERP có giám sát độ mới, bắt buộc ghi nhận đến cuối tháng và đủ chi phí. |
| Tỷ lệ còn dựa trên giả định. | Cường độ tham chiếu dùng monthly_output cố định. Phép tính scenario ở backend và bảng so sánh trong báo cáo kiểm tra giả định về ngày và chi phí; Dashboard không có bộ điều khiển thử giả định. | Sản lượng thực và số đơn vị được kiểm theo sản phẩm, ca và lô, cùng máy và công đoạn, để chuyển cường độ thành tỷ lệ lỗi thật và kiểm chứng nguyên nhân. |

Người phụ trách là vai trò đề xuất và mục tiêu là tạm thời; chưa có việc nào được giao và chưa đo được kết quả thực tế tại nhà máy.

<!-- END GENERATED CANONICAL ANALYSIS -->

## Nguồn dữ liệu, kiến trúc và tài liệu

[Repo đề bài](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026) cung cấp [master CSV](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/blob/main/data/master_data.csv), [template chất lượng dữ liệu](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/blob/main/DATA_QUALITY_TEMPLATE.xlsx) và [endpoint HTTP của bản ghi lỗi](https://raw.githubusercontent.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json). Mã băm SHA-256, commit đối chiếu, thời điểm tải và schema được lưu đầy đủ trong [reports/canonical_snapshot.json](reports/canonical_snapshot.json). Dữ liệu đầu vào gốc luôn ở chế độ chỉ đọc.

`analyst/case_study` đảm nhiệm việc tải dữ liệu, làm sạch, quản lý phạm vi lọc, tính toán chỉ số, kịch bản độ nhạy, xuất workbook và bản tóm tắt chất lượng dữ liệu. `analyst/dashboard.py` cung cấp ngữ cảnh phân tích và dữ liệu bằng chứng dùng chung. Runtime Agent, công cụ định kiểu, lưu trữ SQLite và kiểm duyệt báo cáo nằm trong `analyst/`. `frontend/src` triển khai giao diện song ngữ bằng React + TypeScript.

| Tài liệu | Nội dung |
|---|---|
| [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) | Workbook chất lượng dữ liệu hoàn chỉnh cho Part 1 (10 sheet: Summary, Issues Found, Assumptions, Impact on Results và các sheet đối soát). |
| [analyst/app.py](analyst/app.py) và [frontend/src](frontend/src) | Hợp đồng HTTP/WebSocket và giao diện Dashboard/Agent song ngữ. |
| [evals/README.md](evals/README.md) và [AUDIT.md](AUDIT.md) | Quy trình đánh giá và kết quả kiểm thử theo thời điểm. |
| [knowledge/kpi_registry.yaml](knowledge/kpi_registry.yaml) | Sổ định nghĩa công thức KPI, mẫu số sản lượng tham chiếu, đơn vị và quy tắc xử lý dữ liệu lỗi. |

Tài liệu chi tiết và biên nhận kiểm thử được giữ cục bộ trong `docs/`, nằm ngoài Git. Bản clone mới sử dụng hướng dẫn cài đặt, source, hợp đồng và test đã được lưu trong repository.

Server cục bộ lắng nghe tại `127.0.0.1:8000` và lưu trạng thái phiên, dữ liệu SQLite cùng hồ sơ điều tra trong thư mục `.runtime/` (được Git bỏ qua). Bản [triển khai Cloudflare](cloudflare/README.md) cấp cho mỗi người dùng một Container cô lập gắn với trạng thái Durable Object bền vững, đồng thời giữ `OPENAI_API_KEY` an toàn ở tầng Worker.
