# Nhật ký và bàn giao mac2API

## Bản làm việc ngày 19/11/2026

- **Chromium tự tắt khi rảnh.** `MUSE2API_CHROMIUM_IDLE_SECONDS` mặc định 60 giây. Đồng hồ tính từ lúc thao tác dùng Chromium cuối cùng trả khe `GEN_LOCK`. Vòng kiểm tra không tắt khi còn khe đang dùng hoặc tác vụ ảnh/video đang chờ. Chỉ tắt tiến trình do ứng dụng khởi động. Yêu cầu Muse sau đó khởi động lại theo luồng sẵn có.
- Khi bật chế độ tự tắt, ứng dụng bỏ bước mở Chromium để làm nóng lúc khởi động. Duy trì phiên bằng HTTP theo lịch vẫn hoạt động. OpenAI và Gemini dùng kết nối HTTPS riêng.
- Đặt `MUSE2API_CHROMIUM_IDLE_SECONDS=0` để giữ Chromium chạy theo cách cũ. Với Render Free, có thể đặt thêm `MUSE2API_CONCURRENCY=1` để giảm đỉnh RAM. Chromium có thể mất vài giây để mở lại. Một tác vụ lớn vẫn có thể vượt giới hạn RAM của dịch vụ.
- Nhật ký ứng dụng ghi `Chromium đã tắt sau 60 giây không có tác vụ Muse.` khi quy trình dừng hoàn tất. Kiểm tra `/readyz` để xem `browser_running`, và Render Metrics để đo RAM thật.

- Kiểm thử đã chạy với đồng hồ mô phỏng ở mốc 60 giây và với Chromium thật ở mốc rút gọn 1 giây. Bộ kiểm thử đồng thời, duy trì phiên, proxy, Quota và ảnh bất đồng bộ đều đạt. Chưa đo RAM trên Render.

## Commit `8845100` trên GitHub

- Quota chờ tối đa 60 giây để lấy khóa tài khoản sau khi có khe xử lý. Các thao tác quản trị khác vẫn báo bận ngay khi có xung đột.
- Chromium nhận diện **Cài đặt → Mức sử dụng**. Bộ phân tích đọc gói, phần trăm, ngày đặt lại và token bổ sung bằng tiếng Việt, Anh và Trung. Thiếu số phần trăm tuần hợp lệ thì giữ cache hạn mức cũ.
- Kiểm thử Quota bao phủ định dạng bản địa hóa, chờ khóa và cách ly phiên. Dữ liệu tài khoản dùng trong kiểm thử là dữ liệu giả.

## Commit `51f5332` trên GitHub

- Việt hóa bảng quản trị, tiện ích Chrome/Edge, tập lệnh Tampermonkey, công cụ nhập cookie và tài liệu. Giao diện của người dùng mới mặc định tiếng Việt. Trình duyệt giữ lựa chọn ngôn ngữ đã lưu.
- Thêm **Ngôn ngữ chỉ dẫn Muse** trong **Quản lý tài khoản**. Giá trị lưu ở `data/muse-prompt.json`. Chọn Việt, Anh hoặc Trung cho chỉ dẫn tự ghép vào yêu cầu Muse. Kết quả phản hồi do Muse quyết định.
- Thêm tab OpenAI và Gemini, cấu hình model và khóa riêng. `/v1/chat/completions` định tuyến theo tiền tố model và hỗ trợ SSE. OpenAI/Gemini cần khóa hợp lệ và quyền kết nối tới tên miền API chính thức. Các tên model Muse cũ vẫn được hỗ trợ.
- Proxy HTTP nhận chuỗi `IP:PORT:USER:PASS`. Dùng **Kiểm tra kết nối** trước khi lưu để phân biệt HTTP 407, kết nối và phiên đăng nhập Muse. SOCKS5/SOCKS5h dùng URL có giao thức.
- Kho media tối đa 5 GB. Từ 3,5 GB, tự dọn tệp cũ nhất và ảnh thu nhỏ. Thư viện hiển thị tệp mới nhất trước. Lịch sử tác vụ được giữ lại, còn URL tệp đã dọn sẽ trả 404.

## Vận hành và kiểm thử

- Mã nguồn nằm ở repo `mktminhqpbga-glitch/mac2API`, nhánh `main`. Render triển khai từ GitHub. Đối chiếu SHA trong Render rồi thử `/healthz` và `/admin` để xác nhận một bản cụ thể đã chạy.
- Khi triển khai lại, giữ biến môi trường và ổ dữ liệu nếu dịch vụ có ổ đĩa bền vững. Render Free có thể mất cấu hình hoặc media lưu cục bộ sau deploy hoặc khởi động lại.
- Kiểm thử liên quan gồm `tests/test_concurrency.py`, `tests/test_quota_localization.py`, `tests/test_quota_account_binding.py`, `tests/test_account_proxy_routing.py`, `tests/test_chat_providers.py`, `tests/test_muse_prompt_language.py`, `tests/test_media_storage.py`, `tests/test_proxy_url_import.py` và `node --test tests/*.mjs`.
- MacFlow chưa được cấu hình trong công việc này. Các giá trị kết nối là Base URL của mac2API có hậu tố `/v1`, API Key của mac2API và model ID tương ứng.
